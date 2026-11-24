"""Write half of the UniFi family (stage B: clients, networks, fixed addresses, port forwards). Loaded only by the
executor service, never by the web app.

What it can change - nothing else, whatever a plan says:

* ``move``                move a client to another network: network override plus a fixed address in that network
* ``client.set.network``  the same, as part of a client step that also renames or readdresses the client
* ``client.set.fixed_ip`` give a client a fixed address in its network, change it, or remove it ("")
* ``client.set.name``     the client's name
* ``network.create``      an internal network with a VLAN of its own and private IPv4 addresses (it lands in the
                          internal zone; zones of its own come with the zone firewall stage)
* ``network.update``      a network's name and its address service (on/off, range) - never WAN, VPN or guest networks
* ``network.delete``      only a network this write half created itself, and only when nothing refers to it any more
* ``dhcp.reserve|release`` a fixed address for a known client inside its own network, or release it
* ``forward.create|update|delete`` port forwards to a private address of a local network (never a gateway address);
                          delete only forwards this write half created
* ``zone.create|update|delete`` zones of its own (name only); delete only a zone this write half created, with no
                          network and no rule left in it (the controller would delete such rules without a word)
* ``network.update.zone`` move a local network into a zone of its own or back into the internal zone (also when a new
                          network is created)
* ``policy.create|update|delete|reorder`` rules between zones (any traffic or a few ports, a whole zone or one network
                          of it); change only rules whose every setting Lanloom can carry over; delete only rules this
                          write half created; reorder the own rules of one zone pair
* ``port.set``            a switch port's name, port profile, untagged network, tagged networks (all, none or a choice)
                          and power over Ethernet (automatic mode or off) - never an uplink, an internet port, a port
                          with network equipment or another device announcing itself (LLDP) behind it, a mirror or
                          aggregation port, nor a port that carries a device Lanloom protects or the own path
* ``port.cycle``          switch a port's power off and on again (restarts the device it feeds), with the same rules
* ``ssid.create|update|delete`` wireless networks: name, network, bands, security (open, WPA2, WPA2/WPA3, WPA3 personal),
                          hidden, on/off; delete only an SSID this write half created. Never radio settings (channel,
                          width, transmit power), never enterprise (RADIUS) security, never the network or security of an
                          SSID with private pre-shared keys. A password is never part of a plan: a new protected SSID gets
                          a long random one, read or changed in the controller's own app
* ``client.block``        block or unblock a client (``block-sta`` / ``unblock-sta`` of ``cmd/stamgr``); never a kick

Client settings, networks and port forwards are written over the classic API (the official integration API has no
client or port forward writes, and network writes only on recent releases), so the write half signs in with a local
account of its own - a site administrator without system rights, separate from the read-only account of the app.
What it created is remembered in ``unifi-own.json`` in its state directory: only those networks and port forwards may
ever be deleted. Its options file (mode 0600, see ``credentials.load_write_options``)::

    {"url": "https://192.168.1.1", "site": "default", "write_username": "...", "write_password": "...",
     "cert_sha256": "<SHA-256 of the console's certificate>"}           # or a certificate the system trusts

Safety below the adapter: every request goes through ``transport.Transport`` with a field-exact allow-list (GET on
the areas read below, sign-in/out, ``PUT rest/user/<id>`` with only the six client fields, ``POST/PUT
rest/networkconf`` with only the fields of an internal VLAN network, ``POST/PUT rest/portforward`` with only the fields
of a forward, ``DELETE`` of one network or forward; each value checked). Before anything is written the half checks
what the controller itself does not (``firewall.netconf``): that a network is local, that an address lies inside it, is
not its gateway and is not used by another client, that a new network overlaps nothing, that a forward reaches a
private address. After every write it reads the object back and stops (the executor rolls back) when the values did not
take.

Sessions: one sign-in per need, one new sign-in after HTTP 401 and never after 403, a planned new sign-in before the
controller's own session limit, and at most four sign-ins an hour across processes with a 15-minute pause after a
refused session (``session.FileLedger``). Product names are used only to identify compatible products; no
affiliation or endorsement.

Switch ports are written as the switch's port overrides (``PUT rest/device/<id>`` with the field ``port_overrides``
only): the controller replaces the whole list, so the half sends the list exactly as it read it and changes only the
entry of the planned port, and only its name, profile, untagged network, tagged mode and networks and power mode; a
body that differs anywhere else is refused before it is sent. A power cycle is the command ``power-cycle`` of
``cmd/devmgr`` for one port of one switch.

Zones and rules are written over the classic API of the zone-based firewall (``v2/api/site/<site>/firewall/zone``,
``firewall-policies``, ``firewall-policies/batch-reorder``), with the same account; a rule body always carries the
complete, checked set of fields (the controller does not take partial rule updates). Before a plan with zone or rule
steps runs, the half checks that the path from its own address to the controller stays open (``firewall.zonefw``).

Wireless networks are written over the classic API (``POST/PUT/DELETE rest/wlanconf``) with only the fields of the
allow-list below; blocking a client is ``cmd/stamgr`` with exactly ``cmd`` and ``mac``. The rules of
``firewall.wireless`` run again on the half's own fresh read: no SSID that carries a protected device, the controller or
this executor is switched off, deleted, renamed or moved; no protected device, controller or executor is blocked.
"""
import copy
import ipaddress
import json
import logging
import os
import re
import stat
import time
from pathlib import Path

from . import WRITE_CAPABILITIES, model, session
from .. import capabilities
from ..credentials import Secret, vet_write_account
from ..transport import Forbidden, Rule, Transport, TransportError, write_allowlist
from ...firewall import netconf, portconf, wireless, zonefw
from ...firewall import steps as stepkinds
from ...firewall.adapter import ControllerAdapter

log = logging.getLogger("lanloom.executor")

URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])(?::(\d{1,5}))?/?\Z", re.I)
SITE_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
OID = r"[0-9a-f]{24}"
_S = r"[A-Za-z0-9_-]{1,64}"
_P = r"(?:/proxy/network)?"
_OID_RE = re.compile(r"\A%s\Z" % OID)
MSG_RE = re.compile(r"\A[A-Za-z0-9_.]{1,60}\Z")
SETTLE_WIRELESS_S = 90           # a wireless client reconnects after its network changed
SETTLE_ADDRESS_S = 5
SETTLE_CONFIG_S = 10             # the gateway provisions a network or forward change
VLAN_MAX = 4009                  # the controller keeps 4010 and above for itself
LOCAL_PURPOSES = ("corporate", "guest", "vlan-only", "")
OPTIONS = ("url", "site", "write_username", "write_password", "allow_self_signed", "cert_sha256", "settle_s", "timeout",
           "state_dir")


# ---------------------------------------------------------------------------- the allow-list (below the adapter)
def _name_ok(v):
    return isinstance(v, str) and len(v) <= 128 and not any(ord(c) < 32 or ord(c) == 127 for c in v)


def _ip_or_empty(v):
    if v == "":
        return True
    ipaddress.ip_address(v if isinstance(v, str) else "")
    return True


def _oid_or_empty(v):
    return isinstance(v, str) and (v == "" or bool(_OID_RE.match(v)))


def _bool(v):
    return isinstance(v, bool)


def _ip4(v):
    a = ipaddress.ip_address(v if isinstance(v, str) else "")
    return a.version == 4


def _private_ip4(v):
    a = ipaddress.ip_address(v if isinstance(v, str) else "")
    return a.version == 4 and a.is_private


def _subnet(v):
    """An internal network: gateway address/prefix, private IPv4, /16 to /29 (never 0.0.0.0/0, never public)."""
    n = ipaddress.ip_interface(v if isinstance(v, str) else "")
    return (n.version == 4 and n.network.is_private and netconf.PREFIX_MIN <= n.network.prefixlen <= netconf.PREFIX_MAX
            and n.ip not in (n.network.network_address, n.network.broadcast_address))


def _vlan(v):
    return isinstance(v, int) and not isinstance(v, bool) and netconf.VLAN_FIRST <= v <= VLAN_MAX


def _const(*allowed):
    return lambda v: v in allowed


def _ports(v):
    stepkinds._ports(v if isinstance(v, str) else None)
    return bool(v)


def _ports_or_empty(v):
    stepkinds._ports(v if isinstance(v, str) else None)
    return True


def _short_name(v):
    return _name_ok(v) and bool(v.strip()) and len(v) <= 64


def _oid(v):
    return isinstance(v, str) and bool(_OID_RE.match(v))


def _oid_list(v):
    return isinstance(v, list) and len(v) <= 256 and len(set(v)) == len(v) and all(_oid(x) for x in v)


def _false(v):
    return v is False


def _empty_list(v):
    return v == []


def _always(v):
    return v == {"mode": "ALWAYS"}


def _pol_side(v):
    """One side of a rule: a zone, optionally one network of it, optionally ports (destination only, checked again in
    the body builder); nothing else."""
    if not isinstance(v, dict) or not set(v) <= set(model.SIDE_KEYS) or not _oid(v.get("zone_id")):
        return False
    if v.get("matching_target") not in ("ANY", "NETWORK") or v.get("port_matching_type") not in ("ANY", "SPECIFIC"):
        return False
    if v.get("match_opposite_ports") is not False:
        return False
    if v["matching_target"] == "NETWORK":
        if not (isinstance(v.get("network_ids"), list) and len(v["network_ids"]) == 1 and _oid(v["network_ids"][0])) \
                or v.get("match_opposite_networks") is not False:
            return False
    elif "network_ids" in v or "match_opposite_networks" in v:
        return False
    if v["port_matching_type"] == "SPECIFIC":
        return _ports(v.get("port"))
    return "port" not in v


OVERRIDE_KEY = re.compile(r"\A[a-z0-9_]{1,48}\Z")
MAC_RE = re.compile(r"\A[0-9a-f]{2}(?::[0-9a-f]{2}){5}\Z")
OVERRIDE_ENUMS = {"poe_mode": ("auto", "off", "pasv24", "passthrough"), "tagged_vlan_mgmt": ("auto", "block_all", "custom"),
                  "forward": ("all", "native", "customize", "disabled"), "op_mode": ("switch", "mirror", "aggregate")}
PORT_WRITE_KEYS = frozenset(("name", "portconf_id", "native_networkconf_id", "tagged_vlan_mgmt", "forward",
                             "excluded_networkconf_ids", "poe_mode"))
TAGGED_OUT = {"all": "auto", "none": "block_all", "custom": "custom"}


def _plain(v, depth=0) -> bool:
    """A value of a port override as the controller stores it: numbers, short text, flags, lists of those, one level
    of small objects. Anything else (deep nesting, control characters, long text) is refused."""
    if v is None or isinstance(v, (bool, int, float)):
        return True
    if isinstance(v, str):
        return _name_ok(v)
    if isinstance(v, list):
        return len(v) <= 256 and all(_plain(x, depth + 1) for x in v) and depth < 2
    if isinstance(v, dict):
        return depth < 1 and len(v) <= 32 and all(isinstance(k, str) and OVERRIDE_KEY.match(k) and _plain(x, depth + 1)
                                                  for k, x in v.items())
    return False


def _overrides(v):
    """The port override list of a switch: at most 64 entries, one per port number, known shapes of the fields this
    half may change; the other fields as the controller keeps them (the body builder sends them unchanged)."""
    if not isinstance(v, list) or len(v) > 64:
        return False
    seen = set()
    for o in v:
        if not isinstance(o, dict) or len(o) > 80 or not all(isinstance(k, str) and OVERRIDE_KEY.match(k) for k in o):
            return False
        idx = o.get("port_idx")
        if isinstance(idx, bool) or not isinstance(idx, int) or not 1 <= idx <= 64 or idx in seen:
            return False
        seen.add(idx)
        if not all(_plain(x) for x in o.values()):
            return False
        for k, allowed in OVERRIDE_ENUMS.items():
            if k in o and o[k] not in allowed:
                return False
        if "portconf_id" in o and not _oid_or_empty(o["portconf_id"]):
            return False
        if "native_networkconf_id" in o and not _oid_or_empty(o["native_networkconf_id"]):
            return False
        if "excluded_networkconf_ids" in o and not _oid_list(o["excluded_networkconf_ids"]):
            return False
        if "name" in o and (not isinstance(o["name"], str) or len(o["name"]) > 64):
            return False
    return True


def _port_idx(v):
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 64


def _mac(v):
    return isinstance(v, str) and bool(MAC_RE.match(v))


def _ssid_name(v):
    if not isinstance(v, str) or not v.strip() or len(v) > 32 or any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError("ssid name")


def _passphrase(v):
    """A password the half chose itself: 16 to 63 printable characters without spaces."""
    if not isinstance(v, str) or not 16 <= len(v) <= 63 or any(not 33 <= ord(c) <= 126 for c in v):
        raise ValueError("passphrase")


def _wlan_bands(v):
    if not isinstance(v, list) or not v or len(set(v)) != len(v) or not set(v) <= {"2g", "5g", "6g"}:
        raise ValueError("bands")


def _wlan_mac(v):
    if not isinstance(v, str) or not MAC_RE.match(v):
        raise ValueError("mac")


USER_VALUES = {"name": _name_ok, "use_fixedip": _bool, "fixed_ip": _ip_or_empty, "network_id": _oid_or_empty,
               "virtual_network_override_enabled": _bool, "virtual_network_override_id": _oid_or_empty}
# a new internal network: nothing but these fields, each with its only allowed values (never WAN, VPN or a guest portal)
NET_CREATE_VALUES = {"name": _short_name, "purpose": _const("corporate"), "networkgroup": _const("LAN"), "ip_subnet": _subnet,
                     "vlan_enabled": _const(True), "vlan": _vlan, "dhcpd_enabled": _bool, "dhcpd_start": _private_ip4,
                     "dhcpd_stop": _private_ip4, "enabled": _const(True), "setting_preference": _const("manual"),
                     "internet_access_enabled": _const(True), "ipv6_interface_type": _const("none")}
NET_UPDATE_VALUES = {"name": _short_name, "dhcpd_enabled": _bool, "dhcpd_start": _private_ip4, "dhcpd_stop": _private_ip4}
FWD_VALUES = {"name": _short_name, "enabled": _bool, "src": _const("any"), "dst_port": _ports, "fwd": _private_ip4,
              "fwd_port": _ports_or_empty, "proto": _const("tcp", "udp", "tcp_udp"), "log": _bool,
              "pfwd_interface": _const("wan")}
FWD_UPDATE_VALUES = {k: FWD_VALUES[k] for k in ("name", "enabled", "dst_port", "fwd", "fwd_port", "proto")}
ZONE_CREATE_VALUES = {"name": _short_name, "network_ids": _oid_list}
ZONE_UPDATE_VALUES = {"_id": _oid, "name": _short_name, "network_ids": _oid_list}
# a complete rule: the controller takes rule bodies only whole; every field has its only allowed values
POLICY_VALUES = {"name": _short_name, "enabled": _bool, "action": _const("ALLOW", "BLOCK", "REJECT"),
                 "create_allow_respond": _bool, "logging": _bool, "protocol": _const("all", "tcp", "udp", "tcp_udp"),
                 "ip_version": _const("BOTH", "IPV4"), "connection_state_type": _const("ALL"), "connection_states": _empty_list,
                 "match_ip_sec": _false, "match_opposite_protocol": _false, "icmp_typename": _const("ANY"),
                 "icmp_v6_typename": _const("ANY"), "schedule": _always, "source": _pol_side, "destination": _pol_side}
DEVICE_VALUES = {"port_overrides": _overrides}
CYCLE_VALUES = {"cmd": _const("power-cycle"), "mac": _mac, "port_idx": _port_idx}
# a wireless network: name, network, bands, security (open or a personal password), hidden, on/off - nothing else
WLAN_UPDATE_VALUES = {"name": _ssid_name, "networkconf_id": _oid, "wlan_bands": _wlan_bands, "security": _const("open", "wpapsk"),
                      "wpa_mode": _const("wpa2"), "wpa3_support": _bool, "wpa3_transition": _bool, "hide_ssid": _bool,
                      "enabled": _bool, "x_passphrase": _passphrase}
WLAN_CREATE_VALUES = dict(WLAN_UPDATE_VALUES, wpa_enc=_const("ccmp"), ap_group_ids=_oid_list, ap_group_mode=_const("all"),
                          usergroup_id=_oid)
WLAN_NEEDED = ("name", "networkconf_id", "wlan_bands", "security", "hide_ssid", "enabled")
STAMGR_VALUES = {"cmd": _const("block-sta", "unblock-sta"), "mac": _wlan_mac}     # never kick-sta: a client comes back at once
REORDER_VALUES = {"source_zone_id": _oid, "destination_zone_id": _oid, "before_predefined_ids": _oid_list,
                  "after_predefined_ids": _oid_list}
READ_PATHS = (
    _P + r"/api/s/%s/(?:self|stat/(?:sta|device|sysinfo)|rest/(?:networkconf|user|portforward|wlanconf|portconf))" % _S,
    _P + r"/api/s/%s/rest/(?:user|networkconf|portforward|wlanconf)/%s" % (_S, OID),
    _P + r"/v2/api/site/%s/(?:firewall/zone|firewall-policies)" % _S,
    _P + r"/v2/api/site/%s/firewall-policies/%s" % (_S, OID),
)
ALLOW = write_allowlist(
    [Rule("GET", p) for p in READ_PATHS]
    + [Rule("POST", r"/api/(?:auth/)?login", fields=("username", "password"), login=True),
       Rule("POST", r"/api/(?:auth/)?logout", fields=()),
       Rule("PUT", _P + r"/api/s/%s/rest/user/%s" % (_S, OID), fields=tuple(USER_VALUES), values=USER_VALUES, nonempty=True),
       Rule("POST", _P + r"/api/s/%s/rest/networkconf" % _S, fields=tuple(NET_CREATE_VALUES), values=NET_CREATE_VALUES,
            exact=True),
       Rule("PUT", _P + r"/api/s/%s/rest/networkconf/%s" % (_S, OID), fields=tuple(NET_UPDATE_VALUES),
            values=NET_UPDATE_VALUES, nonempty=True),
       Rule("DELETE", _P + r"/api/s/%s/rest/networkconf/%s" % (_S, OID)),
       Rule("POST", _P + r"/api/s/%s/rest/portforward" % _S, fields=tuple(FWD_VALUES), values=FWD_VALUES, exact=True),
       Rule("PUT", _P + r"/api/s/%s/rest/portforward/%s" % (_S, OID), fields=tuple(FWD_UPDATE_VALUES),
            values=FWD_UPDATE_VALUES, nonempty=True),
       Rule("DELETE", _P + r"/api/s/%s/rest/portforward/%s" % (_S, OID)),
       Rule("POST", _P + r"/v2/api/site/%s/firewall/zone" % _S, fields=tuple(ZONE_CREATE_VALUES), values=ZONE_CREATE_VALUES,
            exact=True),
       Rule("PUT", _P + r"/v2/api/site/%s/firewall/zone/%s" % (_S, OID), fields=tuple(ZONE_UPDATE_VALUES),
            values=ZONE_UPDATE_VALUES, exact=True),
       Rule("DELETE", _P + r"/v2/api/site/%s/firewall/zone/%s" % (_S, OID)),
       Rule("POST", _P + r"/v2/api/site/%s/firewall-policies" % _S, fields=tuple(POLICY_VALUES), values=POLICY_VALUES,
            exact=True),
       Rule("PUT", _P + r"/v2/api/site/%s/firewall-policies/%s" % (_S, OID), fields=tuple(POLICY_VALUES),
            values=POLICY_VALUES, exact=True),
       Rule("DELETE", _P + r"/v2/api/site/%s/firewall-policies/%s" % (_S, OID)),
       Rule("PUT", _P + r"/v2/api/site/%s/firewall-policies/batch-reorder" % _S, fields=tuple(REORDER_VALUES),
            values=REORDER_VALUES, exact=True),
       Rule("PUT", _P + r"/api/s/%s/rest/device/%s" % (_S, OID), fields=tuple(DEVICE_VALUES), values=DEVICE_VALUES, exact=True),
       Rule("POST", _P + r"/api/s/%s/cmd/devmgr" % _S, fields=tuple(CYCLE_VALUES), values=CYCLE_VALUES, exact=True),
       Rule("POST", _P + r"/api/s/%s/rest/wlanconf" % _S, fields=tuple(WLAN_CREATE_VALUES), values=WLAN_CREATE_VALUES,
            nonempty=True),
       Rule("PUT", _P + r"/api/s/%s/rest/wlanconf/%s" % (_S, OID), fields=tuple(WLAN_UPDATE_VALUES), values=WLAN_UPDATE_VALUES,
            nonempty=True),
       Rule("DELETE", _P + r"/api/s/%s/rest/wlanconf/%s" % (_S, OID)),
       Rule("POST", _P + r"/api/s/%s/cmd/stamgr" % _S, fields=tuple(STAMGR_VALUES), values=STAMGR_VALUES, exact=True)])
SECURITY_OUT = {"open": {"security": "open", "wpa3_support": False, "wpa3_transition": False},
                "wpa2_personal": {"security": "wpapsk", "wpa_mode": "wpa2", "wpa3_support": False, "wpa3_transition": False},
                "wpa2_wpa3_personal": {"security": "wpapsk", "wpa_mode": "wpa2", "wpa3_support": True, "wpa3_transition": True},
                "wpa3_personal": {"security": "wpapsk", "wpa_mode": "wpa2", "wpa3_support": True, "wpa3_transition": False}}
BANDS_OUT = {"2.4ghz": "2g", "5ghz": "5g", "6ghz": "6g"}
PASS_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"     # no look-alikes (0/O, 1/l/I)
LOGIN = {"console": "/api/auth/login", "classic": "/api/login"}
LOGOUT = {"console": "/api/auth/logout", "classic": "/api/logout"}
PREFIX = {"console": "/proxy/network", "classic": ""}
AREAS = {"sysinfo": "api/s/{site}/stat/sysinfo", "networkconf": "api/s/{site}/rest/networkconf",
         "user": "api/s/{site}/rest/user", "sta": "api/s/{site}/stat/sta", "device": "api/s/{site}/stat/device",
         "portforward": "api/s/{site}/rest/portforward", "wlanconf": "api/s/{site}/rest/wlanconf",
         "portconf": "api/s/{site}/rest/portconf",
         "zone": "v2/api/site/{site}/firewall/zone", "policies": "v2/api/site/{site}/firewall-policies"}
NEEDED = ("networkconf", "user")
CLIENT_KINDS = ("move", "client.set", "dhcp.reserve", "dhcp.release")
ZONE_AREA = "v2/api/site/{site}/firewall/zone"
POLICY_AREA = "v2/api/site/{site}/firewall-policies"
ACTIONS_OUT = {"allow": "ALLOW", "block": "BLOCK", "reject": "REJECT"}
PROTOCOLS_OUT = {"any": "all", "tcp": "tcp", "udp": "udp", "tcp_udp": "tcp_udp"}


class OwnLedger:
    """Classic ids of the networks and port forwards this write half created (``unifi-own.json``, 0600, never through
    a symbolic link). Only those may ever be deleted - by a plan or by the way back."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.data = {"networkconf": [], "portforward": [], "zone": [], "policy": [], "wlanconf": []}
        if self.path is not None:
            try:
                fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as fh:
                    if stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                        got = json.loads(fh.read().decode("utf-8") or "{}")
                        for k in self.data:
                            self.data[k] = [x for x in got.get(k) or [] if isinstance(x, str) and _OID_RE.match(x)]
            except (OSError, ValueError):
                pass

    def has(self, area: str, vendor_id: str) -> bool:
        return vendor_id in self.data.get(area, [])

    def _save(self):
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(self.data, sort_keys=True).encode("utf-8"))
        os.replace(tmp, self.path)

    def add(self, area: str, vendor_id: str):
        if vendor_id not in self.data[area]:
            self.data[area].append(vendor_id)
            self._save()

    def drop(self, area: str, vendor_id: str):
        if vendor_id in self.data[area]:
            self.data[area].remove(vendor_id)
            self._save()


class WriteError(RuntimeError):
    """A write that cannot or did not happen. ``key`` is a short reason (never a secret, never an address)."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(key)
        self.key, self.detail = key, detail


def _msg(body) -> str:
    meta = body.get("meta") if isinstance(body, dict) else None
    text = str((meta or {}).get("msg") or "")
    return text if MSG_RE.match(text) else ""


class UnifiWriter(ControllerAdapter):
    label = "UniFi Network (local API)"
    kind = "controller"
    family = "unifi"

    def __init__(self, url=None, site="default", write_username=None, write_password=None, allow_self_signed=False,
                 cert_sha256=None, settle_s=None, timeout=15.0, state_dir=None, *, clock=time.time, sleep=time.sleep, **unknown):
        if unknown:
            raise ValueError("unknown option(s): %s (known: %s)" % (", ".join(sorted(unknown)), ", ".join(OPTIONS)))
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise ValueError("url: https://<address of the console or controller>")
        self._host = m.group(2).strip("[]")
        self._port = int(m.group(3)) if m.group(3) else (443 if m.group(1).lower() == "https" else 80)
        base = "%s://%s%s" % (m.group(1).lower(), m.group(2), ":" + m.group(3) if m.group(3) else "")
        site = str(site or "default").strip()
        if not SITE_RE.match(site):
            raise ValueError("site")
        self._site = site
        self._user = str(write_username or "").strip()
        self._password = write_password if isinstance(write_password, Secret) else Secret(write_password)
        if not self._user or not self._password or len(self._user) > 128 or any(ord(c) < 32 for c in self._user):
            raise ValueError("write_username and write_password are needed")
        if allow_self_signed and not cert_sha256:
            log.warning("unifi: allow_self_signed without cert_sha256 - the connection is not bound to the console's "
                        "certificate; set cert_sha256 (see docs/integrations/unifi.md)")
        self._t = Transport(base, ALLOW, allow_self_signed=bool(allow_self_signed), timeout=float(timeout),
                            pin_sha256=cert_sha256 or None)
        self._session = session.Session(session._key(self._host, self._user, self._password.reveal()))
        ledger = Path(state_dir) / "unifi-signins.json" if state_dir else None
        self._ledger = session.FileLedger(ledger, per_hour=WRITE_CAPABILITIES["limits"]["logins_per_hour"], clock=clock)
        self._own = OwnLedger(Path(state_dir) / "unifi-own.json" if state_dir else None)
        self._fwds = {}
        self._zones = {}
        self._pols = {}
        self._ports = {}
        self._profiles = {}
        self._ssids = {}
        self._macs = {}
        self._created = {}                     # "new:" id of a plan step -> neutral id of the object it created
        self._relogin_free = False
        self._dead = None
        self._account_ok = None
        self.settle_s = SETTLE_WIRELESS_S if settle_s is None else max(0, int(settle_s))
        self.clock, self.sleep = clock, sleep
        self._raw = {}
        self._state = None
        self._refs = {}
        self._nets = {}
        self._probe_fail = 0
        self.writes = []                       # (user id, fields) of every PUT, for the protocol of tests

    def __repr__(self):
        return "UnifiWriter(%s, site=%r)" % (self._host, self._site)

    __str__ = __repr__

    # ------------------------------------------------------------------ contract basics
    def capabilities(self) -> dict:
        return capabilities.validate(WRITE_CAPABILITIES, self.family)

    def endpoints(self) -> list:
        return [self._host]

    # ------------------------------------------------------------------ session
    def _send(self, method: str, path: str, body=None):
        try:
            status, heads, data = self._t.request(method, path, body, headers=self._session.headers())
        except Forbidden:
            raise WriteError("unifi.refused_by_allow_list") from None
        except TransportError as exc:
            raise WriteError(exc.key) from None
        self._session.absorb(heads)
        parsed = None
        if data:
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
        return status, parsed

    def _sign_in(self, allow_relogin: bool = True) -> None:
        if self._dead:
            raise WriteError("unifi.session_refused")
        try:
            self._ledger.book()
        except session.LoginPaused:
            raise WriteError("unifi.sign_in_paused") from None
        s = self._session
        style_known = s.style
        s.forget()
        order = [style_known] if style_known else ["console", "classic"]
        creds = {"username": self._user, "password": self._password.reveal()}
        refused = False
        for i, style in enumerate(order):
            status, body = self._send("POST", LOGIN[style], creds)
            more = i + 1 < len(order)
            if more and status in (401, 404, 405):
                refused = refused or status == 401
                continue
            if status in (400, 401, 403) or (refused and status in (404, 405)):
                self._ledger.refused()
                raise WriteError("unifi.sign_in_refused")
            if not 200 <= status < 300 or not s.headers().get("Cookie"):
                raise WriteError("unifi.sign_in_failed")
            s.started(style)
            self._relogin_free = allow_relogin
            return
        raise WriteError("unifi.sign_in_failed")

    def _abort(self, why: str):
        self._dead = why
        self._session.forget()
        try:
            self._ledger.refused()
        except OSError:
            pass
        raise WriteError("unifi.session_refused")

    def _call(self, method: str, rel: str, body=None):
        """One request with at most one new sign-in after 401; a second 401 ends this session for good (no loop)."""
        if self._dead:
            raise WriteError("unifi.session_refused")
        if not self._session.signed_in:
            self._sign_in()
        path = "%s/%s" % (PREFIX[self._session.style], rel.format(site=self._site))
        status, data = self._send(method, path, body)
        if status == 401:
            if not self._relogin_free:
                self._abort("401")
            self._relogin_free = False
            self._sign_in(allow_relogin=False)
            path = "%s/%s" % (PREFIX[self._session.style], rel.format(site=self._site))
            status, data = self._send(method, path, body)
            if status == 401:
                self._abort("401 after a fresh sign-in")
        if status == 403:
            raise WriteError("unifi.forbidden")             # the account lacks a right; signing in again cannot help
        return status, data

    def _get(self, area: str, optional: bool = True):
        status, body = self._call("GET", AREAS[area])
        if status in (400, 404, 405) and optional:
            return None
        meta = body.get("meta") if isinstance(body, dict) else None
        if not 200 <= status < 300 or (isinstance(meta, dict) and meta.get("rc") not in (None, "ok")):
            if "LoginRequired" in _msg(body):
                self._session.forget()
            raise WriteError("unifi.read_failed", _msg(body))
        return body

    def _user_record(self, uid: str) -> dict:
        status, body = self._call("GET", "api/s/{site}/rest/user/" + uid)
        data = body.get("data") if isinstance(body, dict) else None
        if status != 200 or not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise WriteError("unifi.client_gone", _msg(body))
        return {f: data[0].get(f) for f in model.USER_FIELDS if f in data[0]}

    def sign_out(self) -> None:
        s = self._session
        if not s.signed_in:
            return
        try:
            self._send("POST", LOGOUT[s.style], {})
        except WriteError:
            pass
        finally:
            s.forget()

    # ------------------------------------------------------------------ account check
    def account(self) -> tuple:
        """(ok, warnings) for the write account: refused when it cannot write; owner-level roles give a warning."""
        status, body = self._call("GET", "api/s/{site}/self")
        data = body.get("data") if isinstance(body, dict) else None
        me = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        if str(me.get("site_role") or "") == "readonly":      # a view-only role on the site cannot write, whatever
            return False, ["read_only"]                       # other flags the controller reports (#21)
        role = "superadmin" if me.get("is_super") else ("owner" if me.get("is_owner") else str(me.get("site_role") or ""))
        return vet_write_account({"principal": self._user, "role": role})

    # ------------------------------------------------------------------ reading
    def _read_raw(self) -> dict:
        raw = {}
        for area in AREAS:
            body = self._get(area, optional=area not in NEEDED)
            if body is not None:
                raw[area] = body
        return raw

    def _index(self, raw: dict) -> None:
        self._nets = model.network_refs(raw)
        self._refs = model.client_refs(raw)
        self._fwds = model.forward_refs(raw)
        self._zones = model.zone_refs(raw)
        self._pols = model.policy_refs(raw)
        self._ports = model.port_refs(raw)
        self._profiles = model.profile_refs(raw)
        self._ssids = model.ssid_refs(raw)
        self._macs = model.client_macs(raw)

    def read(self) -> dict:
        raw = self._read_raw()
        state = model.to_neutral(raw)
        state["controller"] = dict(state.get("controller") or {}, label=self.label, kind="controller")
        state["side_effects"] = {"wireless_move": "reprovision_all"}
        self._raw = raw
        self._index(raw)
        for n in state.get("networks") or []:            # only what this write half created may ever be deleted
            ref = self._nets.get(n["id"])
            if ref and self._own.has("networkconf", ref["id"]):
                n["own"] = True
        for f in state.get("port_forwards") or []:
            ref = self._fwds.get(f["id"])
            if ref and self._own.has("portforward", ref["id"]):
                f["own"] = True
        for z in state.get("zones") or []:
            ref = self._zones.get(z["id"])
            if ref and self._own.has("zone", ref["id"]):
                z["own"] = True
        for p in state.get("policies") or []:
            ref = self._pols.get(p["id"])
            if ref and self._own.has("policy", ref["id"]):
                p["own"] = True
        for w in state.get("ssids") or []:
            ref = self._ssids.get(w["id"])
            if ref and self._own.has("wlanconf", ref["id"]):
                w["own"] = True
        self._state = state
        return state

    def health(self) -> tuple:
        if self._dead:
            return False, "session refused"
        if self.clock() < self._ledger.paused_until():
            return False, "sign-in paused"
        try:
            self._get("sysinfo")
            if self._account_ok is None:
                ok, warnings = self.account()
                self._account_ok = ok
                if "owner_role" in warnings:
                    log.warning("unifi: the write account has an owner-level role; a site administrator without system "
                                "rights is enough (see docs/integrations/unifi.md)")
            if not self._account_ok:
                return False, "account cannot write"
        except WriteError as exc:
            return False, exc.key
        return True, ""

    # ------------------------------------------------------------------ plan steps -> client settings
    def _net(self, nid: str) -> dict:
        ref = self._nets.get(nid)
        if not ref:
            raise WriteError("unifi.network_unknown")
        if ref["purpose"] not in LOCAL_PURPOSES or not ref["cidr"]:
            raise WriteError("unifi.network_not_local")
        return ref

    def _check_address(self, ip: str, net: dict, uid: str) -> None:
        addr = ipaddress.ip_address(ip)
        cidr = ipaddress.ip_network(net["cidr"], strict=False)
        if addr not in cidr or addr in (cidr.network_address, cidr.broadcast_address) or ip == net.get("gateway_ip"):
            raise WriteError("unifi.address_outside")
        for other in self._refs.values():
            rec = other["record"]
            if other["user"] != uid and rec.get("use_fixedip") and rec.get("fixed_ip") == ip:
                raise WriteError("unifi.address_taken")
        for dev in (self._state or {}).get("devices") or []:
            ref = self._refs.get(dev["id"]) or {}
            if ref.get("user") != uid and dev.get("ip") == ip and dev.get("online", True):
                raise WriteError("unifi.address_taken")

    def body_for(self, step: dict) -> tuple:
        """(classic user id, the client fields this step writes) for client steps (moves, client settings, fixed
        addresses). Raises WriteError when it must not be written."""
        kind = step["kind"]
        if kind == "dhcp.reserve":
            return self._reserve_body(step)
        if kind == "dhcp.release":
            ref = self._reservation_ref(step["object"])
            return ref["user"], {"use_fixedip": False}
        if kind not in ("move", "client.set"):
            raise WriteError("unifi.kind_not_supported")
        oid = stepkinds.step_object(step)
        ref = self._refs.get(oid)
        if not ref:
            raise WriteError("unifi.client_unknown")
        after = step["after"]
        dev = next((d for d in (self._state or {}).get("devices") or [] if d["id"] == oid), {})
        body = {}
        if kind == "move" or "network" in after:
            if ref.get("wired"):
                raise WriteError("unifi.wired_move")
            net = self._net(after["network"])
            body.update(virtual_network_override_enabled=True, virtual_network_override_id=net["id"])
            ip = after["ip"] if kind == "move" else after.get("fixed_ip", "")
            if ip:
                self._check_address(ip, net, ref["user"])
                body.update(use_fixedip=True, fixed_ip=ip, network_id=net["id"])
        if kind == "client.set":
            if "name" in after:
                body["name"] = after["name"]
            if "fixed_ip" in after and "network" not in after:
                if after["fixed_ip"]:
                    net = self._net(dev.get("network") or "")
                    self._check_address(after["fixed_ip"], net, ref["user"])
                    body.update(use_fixedip=True, fixed_ip=after["fixed_ip"], network_id=net["id"])
                else:
                    body["use_fixedip"] = False
        if not body:
            raise WriteError("unifi.nothing_to_write")
        return ref["user"], body

    def _reservation_ref(self, rid: str) -> dict:
        ref = next((r for r in self._refs.values() if r.get("reservation") == rid), None)
        if not ref:
            raise WriteError("unifi.client_unknown")
        return ref

    def _reserve_body(self, step: dict) -> tuple:
        after = step["after"]
        ref = self._refs.get(after.get("client"))
        if not ref:
            raise WriteError("unifi.client_unknown")
        dev = next((d for d in (self._state or {}).get("devices") or [] if d["id"] == after["client"]), {})
        net = self._net(dev.get("network") or "")
        self._check_address(after["ip"], net, ref["user"])
        return ref["user"], {"use_fixedip": True, "fixed_ip": after["ip"], "network_id": net["id"]}

    # ------------------------------------------------------------------ plan steps -> networks and port forwards
    def _default_zone(self):
        return netconf.default_zone(self._state or {})

    def _network_body(self, step: dict) -> dict:
        """The complete body of a new internal network (every field of the allow-list, each checked there again)."""
        after = step["after"]
        cidr = ipaddress.ip_network(after["cidr"], strict=False)
        dhcp = after.get("dhcp") or {"on": False, "range": None}
        rng = dhcp.get("range") or [str(cidr.network_address + min(6, cidr.num_addresses - 3)), str(cidr.broadcast_address - 1)]
        return {"name": after["name"], "purpose": "corporate", "networkgroup": "LAN",
                "ip_subnet": "%s/%d" % (cidr.network_address + 1, cidr.prefixlen), "vlan_enabled": True, "vlan": after["vlan"],
                "dhcpd_enabled": bool(dhcp.get("on")), "dhcpd_start": rng[0], "dhcpd_stop": rng[1], "enabled": True,
                "setting_preference": "manual", "internet_access_enabled": True, "ipv6_interface_type": "none"}

    @staticmethod
    def _network_update(after: dict) -> dict:
        body = {}
        if "name" in after:
            body["name"] = after["name"]
        if "dhcp" in after:
            body["dhcpd_enabled"] = bool(after["dhcp"].get("on"))
            if after["dhcp"].get("range"):
                body["dhcpd_start"], body["dhcpd_stop"] = after["dhcp"]["range"]
        if set(after) - {"name", "dhcp", "zone"}:
            raise WriteError("unifi.field_not_supported")
        return body

    @staticmethod
    def _forward_body(after: dict, full: bool) -> dict:
        names = {"name": "name", "enabled": "enabled", "wan_port": "dst_port", "dst_ip": "fwd", "dst_port": "fwd_port", "proto": "proto"}
        body = {names[k]: v for k, v in after.items() if k in names}
        if full:
            body.setdefault("fwd_port", body.get("dst_port", ""))
            if not body["fwd_port"]:
                body["fwd_port"] = body["dst_port"]
            body.update(src="any", log=False, pfwd_interface="wan")
            body.setdefault("enabled", True)
            body.setdefault("proto", "tcp_udp")
        elif "fwd_port" in body and not body["fwd_port"]:
            body["fwd_port"] = after.get("wan_port") or ""
        return body

    def _fresh(self, area: str) -> dict:
        """One list read fresh into the raw replies and the id maps."""
        body = self._get(area, optional=area not in NEEDED)
        raw = dict(self._raw)
        if body is not None:
            raw[area] = body
        self._raw = raw
        self._index(raw)
        return raw

    def _record(self, area: str, vendor_id: str):
        status, body = self._call("GET", "api/s/{site}/rest/%s/%s" % (area, vendor_id))
        data = body.get("data") if isinstance(body, dict) else None
        if status in (400, 404) or (status == 200 and isinstance(data, list) and not data) or "IdInvalid" in _msg(body):
            return None
        if status != 200 or not isinstance(data, list) or not isinstance(data[0], dict):
            raise WriteError("unifi.read_failed", _msg(body))
        return data[0]

    def _write(self, method: str, area: str, vendor_id: str = None, body=None):
        rel = "api/s/{site}/rest/" + area + ("/" + vendor_id if vendor_id else "")
        status, reply = self._call(method, rel, body)
        meta = reply.get("meta") if isinstance(reply, dict) else None
        if not 200 <= status < 300 or (isinstance(meta, dict) and meta.get("rc") not in (None, "ok")):
            raise WriteError("unifi.write_refused", _msg(reply))
        self.writes.append((vendor_id or area, sorted(body) if isinstance(body, dict) else method))
        data = reply.get("data") if isinstance(reply, dict) else None
        return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}

    def _neutral_net_id(self, record: dict) -> str:
        return model.oid("n", record.get("external_id") or record["_id"])

    def _create_network(self, step: dict) -> dict:
        body = self._network_body(step)
        for ref in self._nets.values():                 # already there (a repeated step): nothing to write
            rec = ref.get("record") or {}
            if rec.get("ip_subnet") == body["ip_subnet"] and rec.get("name") == body["name"] and ref.get("vlan") == body["vlan"] \
                    and self._own.has("networkconf", ref["id"]):
                return {"id": next(k for k, v in self._nets.items() if v is ref)}
        made = self._write("POST", "networkconf", body=body)
        vid = str(made.get("_id") or "")
        if not _OID_RE.match(vid):
            raise WriteError("unifi.write_refused", "no id")
        self._own.add("networkconf", vid)              # remembered before anything else can go wrong
        rec = self._record("networkconf", vid)
        if rec is None or not self._matches(rec, {k: body[k] for k in ("name", "ip_subnet", "vlan", "dhcpd_enabled")}):
            raise WriteError("unifi.not_applied")
        nid = self._neutral_net_id(rec)
        self._created[step["object"]] = nid
        self._fresh("networkconf")
        zone = step["after"].get("zone")
        if zone is not None and zone != self._internal_zone():
            self._move_network_zone(nid, zone)
        return {"id": nid}

    def _create_forward(self, step: dict) -> dict:
        body = self._forward_body(step["after"], full=True)
        made = self._write("POST", "portforward", body=body)
        vid = str(made.get("_id") or "")
        if not _OID_RE.match(vid):
            raise WriteError("unifi.write_refused", "no id")
        self._own.add("portforward", vid)
        rec = self._record("portforward", vid)
        if rec is None or not self._matches(rec, {k: body[k] for k in ("name", "dst_port", "fwd", "proto", "enabled")}):
            raise WriteError("unifi.not_applied")
        fid = model.oid("f", vid)
        self._created[step["object"]] = fid
        self._fresh("portforward")
        return {"id": fid}

    def _net_ref(self, nid: str) -> dict:
        ref = self._nets.get(nid)
        if not ref:
            raise WriteError("unifi.network_unknown")
        return ref

    def _fwd_ref(self, fid: str) -> dict:
        ref = self._fwds.get(fid)
        if not ref:
            raise WriteError("unifi.forward_unknown")
        return ref

    def _delete(self, area: str, vendor_id: str) -> None:
        if not self._own.has(area, vendor_id):
            raise WriteError("unifi.not_own")             # nothing Lanloom did not create itself is ever deleted
        if self._record(area, vendor_id) is not None:
            self._write("DELETE", area, vendor_id)
            if self._record(area, vendor_id) is not None:
                raise WriteError("unifi.not_applied")
        self._own.drop(area, vendor_id)

    def _update(self, area: str, vendor_id: str, body: dict) -> None:
        rec = self._record(area, vendor_id)
        if rec is None:
            raise WriteError("unifi.not_applied")
        if self._matches(rec, body):
            return                                        # already as planned
        self._write("PUT", area, vendor_id, body)
        rec = self._record(area, vendor_id)
        if rec is None or not self._matches(rec, body):
            raise WriteError("unifi.not_applied")

    @staticmethod
    def _matches(record: dict, body: dict) -> bool:
        def same(k, v):
            now = record.get(k, "" if isinstance(v, str) else (False if isinstance(v, bool) else None))
            if k in ("vlan",) and not isinstance(v, bool):          # stored as a number or as text, depending on release
                return str(now) == str(v)
            return now == v
        return all(same(k, v) for k, v in body.items())

    def _put(self, uid: str, body: dict) -> None:
        status, reply = self._call("PUT", "api/s/{site}/rest/user/" + uid, body)
        meta = reply.get("meta") if isinstance(reply, dict) else None
        if not 200 <= status < 300 or (isinstance(meta, dict) and meta.get("rc") not in (None, "ok")):
            raise WriteError("unifi.write_refused", _msg(reply))
        self.writes.append((uid, sorted(body)))
        if not self._matches(self._user_record(uid), body):
            raise WriteError("unifi.not_applied")

    def snapshot(self, ids) -> dict:
        """Everything a plan touches as stored now, read fresh (the way back writes exactly these): client settings
        (also behind a fixed address), networks and port forwards. Ids of objects a plan creates have no backup."""
        raw = dict(self._raw)
        raw["user"] = self._get("user", optional=False)
        raw["networkconf"] = self._get("networkconf", optional=False)
        fwd = self._get("portforward")
        if fwd is not None:
            raw["portforward"] = fwd
        self._raw = raw
        self._index(raw)
        res_ids = {r["reservation"]: cid for cid, r in self._refs.items() if r.get("reservation")}
        out = {"devices": [], "dhcp_reservations": [], "networks": [], "port_forwards": []}
        real = [oid for oid in ids if not stepkinds.NEW_ID.match(str(oid))]
        if any(oid in self._zones or oid in self._pols or oid in self._nets for oid in real) or len(real) < len(list(ids)):
            # zones (with the networks in them) and every own rule of the site: the way back of a zone or rule step,
            # of a network moved between zones and of a new network placed into a zone
            for area in ("zone", "policies"):
                body = self._get(area)
                if body is not None:
                    raw[area] = body
            self._raw = raw
            self._index(raw)
            out["zones"] = [{"id": zid, "ref": r["id"], "own": self._own.has("zone", r["id"]), "builtin": r["builtin"],
                             "record": dict(r["record"])} for zid, r in self._zones.items()]
            out["policies"] = [{"id": pid, "ref": r["id"], "own": self._own.has("policy", r["id"]), "record": dict(r["record"])}
                               for pid, r in self._pols.items() if not r["record"].get("predefined")]
        if any(oid in self._ssids for oid in real) or len(real) < len(list(ids)):
            body = self._get("wlanconf")
            if body is not None:
                raw["wlanconf"] = body
                self._raw = raw
                self._index(raw)
            out["ssids"] = [{"id": oid, "ref": self._ssids[oid]["id"], "own": self._own.has("wlanconf", self._ssids[oid]["id"]),
                             "record": copy.deepcopy(self._ssids[oid]["record"])} for oid in real if oid in self._ssids]
        if any(oid in self._ports for oid in real):
            body = self._get("device", optional=False)
            raw["device"] = body
            self._raw = raw
            self._index(raw)
            out["ports"] = [{"id": oid, "device": self._ports[oid]["device"], "idx": self._ports[oid]["idx"],
                             "override": copy.deepcopy(self._ports[oid]["override"])} for oid in real if oid in self._ports]
        for oid in ids:
            if stepkinds.NEW_ID.match(str(oid)):
                continue
            if oid in self._zones or oid in self._pols or oid in self._ports or oid in self._ssids:
                continue
            if oid in self._refs:
                ref = self._refs[oid]
                out["devices"].append({"id": oid, "user": ref["user"], "record": self._user_record(ref["user"]),
                                       "blocked": self._blocked_now(ref["user"])})
            elif oid in res_ids:
                ref = self._refs[res_ids[oid]]
                out["dhcp_reservations"].append({"id": oid, "user": ref["user"], "record": self._user_record(ref["user"])})
            elif oid in self._nets:
                ref = self._nets[oid]
                out["networks"].append({"id": oid, "ref": ref["id"], "own": self._own.has("networkconf", ref["id"]),
                                        "record": dict(ref.get("record") or {})})
            elif oid in self._fwds:
                ref = self._fwds[oid]
                out["port_forwards"].append({"id": oid, "ref": ref["id"], "own": self._own.has("portforward", ref["id"]),
                                             "record": dict(ref.get("record") or {})})
            else:
                raise WriteError("unifi.object_unknown")
        return {k: v for k, v in out.items() if v or k in ("devices", "zones", "policies", "ports", "ssids") and k in out}

    def apply(self, step: dict):
        kind = step["kind"]
        if kind in CLIENT_KINDS:
            uid, body = self.body_for(step)
            if not self._matches(self._user_record(uid), body):
                self._put(uid, body)
            if kind == "dhcp.reserve":
                rid = self._refs[step["after"]["client"]]["reservation"]
                self._created[step["object"]] = rid
                return {"id": rid}
            return None
        if kind == "network.create":
            return self._create_network(step)
        if kind == "port.set":
            self._set_port(step)
            return None
        if kind == "port.cycle":
            self._cycle_port(step["object"])
            return None
        if kind == "network.update":
            ref = self._net_ref(step["object"])
            if ref["purpose"] not in LOCAL_PURPOSES:
                raise WriteError("unifi.network_not_local")
            plain = {k: v for k, v in step["after"].items() if k != "zone"}
            if plain:
                self._update("networkconf", ref["id"], self._network_update(plain))
            if "zone" in step["after"]:
                self._move_network_zone(step["object"], step["after"]["zone"])
            return None
        if kind == "zone.create":
            return self._create_zone(step)
        if kind == "zone.update":
            ref = self._zone_ref(step["object"])
            if ref["builtin"]:
                raise WriteError("unifi.zone_builtin")
            self._put_zone(ref, name=step["after"]["name"])
            return None
        if kind == "zone.delete":
            self._delete_zone(step["object"])
            return None
        if kind == "policy.create":
            return self._create_policy(step)
        if kind == "policy.update":
            self._update_policy(step)
            return None
        if kind == "policy.delete":
            self._delete_policy(step["object"])
            return None
        if kind == "policy.reorder":
            self._reorder(step["object"], step["after"]["order"])
            return None
        if kind == "network.delete":
            ref = self._net_ref(step["object"])
            self._fresh("user")
            st = model.to_neutral(self._raw)
            if any(netconf.references(st, step["object"]).values()):
                raise WriteError("unifi.network_in_use")
            self._delete("networkconf", ref["id"])
            return None
        if kind == "forward.create":
            return self._create_forward(step)
        if kind == "forward.update":
            ref = self._fwd_ref(step["object"])
            self._update("portforward", ref["id"], self._forward_body(step["after"], full=False))
            return None
        if kind == "forward.delete":
            self._delete("portforward", self._fwd_ref(step["object"])["id"])
            return None
        if kind == "ssid.create":
            return self._create_ssid(step)
        if kind == "ssid.update":
            self._update_ssid(step["object"], step["after"])
            return None
        if kind == "ssid.delete":
            self._delete("wlanconf", self._ssid_ref(step["object"])["id"])
            return None
        if kind == "client.block":
            self._block(step["object"], bool(step["after"]["blocked"]))
            return None
        raise WriteError("unifi.kind_not_supported")

    # ------------------------------------------------------------------ plan steps -> wireless networks and blocks
    def _ssid_ref(self, sid: str) -> dict:
        ref = self._ssids.get(sid)
        if not ref:
            raise WriteError("unifi.ssid_unknown")
        return ref

    @staticmethod
    def _new_password() -> str:
        import secrets
        return "".join(secrets.choice(PASS_ALPHABET) for _ in range(24))

    def _wlan_net(self, nid: str) -> str:
        ref = self._nets.get(nid)
        if not ref:
            raise WriteError("unifi.network_unknown")
        if ref["purpose"] not in LOCAL_PURPOSES:
            raise WriteError("unifi.network_not_local")
        return ref["id"]

    def _wlan_body(self, after: dict, record: dict = None, has_password: bool = False) -> dict:
        """The classic fields of an SSID step (``record``: the SSID as stored now, None for a new one). A password goes
        in only where the SSID gets a protected security and has none yet: a random one, never from a plan."""
        rec = record or {}
        if rec.get("security") == "wpaeap" and "security" in after:
            raise WriteError("unifi.ssid_enterprise")
        if rec.get("private_preshared_keys_enabled") and ("security" in after or "network" in after):
            raise WriteError("unifi.ssid_private_keys")
        body = {}
        if "name" in after:
            body["name"] = after["name"]
        if "network" in after:
            body["networkconf_id"] = self._wlan_net(after["network"])
        if "bands" in after:
            body["wlan_bands"] = [BANDS_OUT[b] for b in after["bands"]]
        if "hidden" in after:
            body["hide_ssid"] = bool(after["hidden"])
        if "enabled" in after:
            body["enabled"] = bool(after["enabled"])
        if "security" in after:
            body.update(SECURITY_OUT[after["security"]])
            if after["security"] != "open" and (record is None or not has_password):
                body["x_passphrase"] = self._new_password()
        if set(after) - {"name", "network", "bands", "hidden", "enabled", "security"}:
            raise WriteError("unifi.field_not_supported")
        return body

    @staticmethod
    def _wlan_matches(record: dict, body: dict) -> bool:
        def same(k, v):
            now = record.get(k)
            if k == "wlan_bands":
                return sorted(now or []) == sorted(v)
            if isinstance(v, bool):
                return bool(now) == v
            return now == v
        return all(same(k, v) for k, v in body.items() if k != "x_passphrase")

    def _create_ssid(self, step: dict) -> dict:
        after = dict(wireless.SSID_DEFAULTS, **step["after"])
        body = self._wlan_body(after)
        if body.get("security") == "wpapsk":
            body["wpa_enc"] = "ccmp"
        # where the controller keeps access point groups and a user group per SSID, a new one takes those of the
        # existing SSIDs (all access points; no bandwidth limit of its own)
        for ref in self._ssids.values():
            rec = ref["record"]
            if isinstance(rec.get("ap_group_ids"), list) and rec["ap_group_ids"] and all(_OID_RE.match(str(x)) for x in rec["ap_group_ids"]):
                body.update(ap_group_ids=list(rec["ap_group_ids"]), ap_group_mode="all")
            if _OID_RE.match(str(rec.get("usergroup_id") or "")):
                body["usergroup_id"] = rec["usergroup_id"]
            if "ap_group_ids" in body or "usergroup_id" in body:
                break
        for sid, ref in self._ssids.items():           # already there (a repeated step): nothing to write
            if ref["record"].get("name") == body["name"] and self._own.has("wlanconf", ref["id"]):
                self._created[step["object"]] = sid
                return {"id": sid}
        made = self._write("POST", "wlanconf", body=body)
        vid = str(made.get("_id") or "")
        if not _OID_RE.match(vid):
            raise WriteError("unifi.write_refused", "no id")
        self._own.add("wlanconf", vid)                 # remembered before anything else can go wrong
        rec = self._record("wlanconf", vid)
        if rec is None or not self._wlan_matches(rec, body):
            raise WriteError("unifi.not_applied")
        sid = model.oid("s", rec.get("external_id") or vid)
        self._created[step["object"]] = sid
        self._fresh("wlanconf")
        return {"id": sid}

    def _update_ssid(self, sid: str, after: dict) -> None:
        ref = self._ssid_ref(sid)
        rec = self._record("wlanconf", ref["id"])
        if rec is None:
            raise WriteError("unifi.not_applied")
        body = self._wlan_body(after, rec, bool(rec.get("x_passphrase")))
        if self._wlan_matches(rec, body) and "x_passphrase" not in body:
            return                                        # already as planned
        self._write("PUT", "wlanconf", ref["id"], body)
        rec = self._record("wlanconf", ref["id"])
        if rec is None or not self._wlan_matches(rec, body):
            raise WriteError("unifi.not_applied")

    def _blocked_now(self, uid: str) -> bool:
        status, body = self._call("GET", "api/s/{site}/rest/user/" + uid)
        data = body.get("data") if isinstance(body, dict) else None
        if status != 200 or not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise WriteError("unifi.client_gone", _msg(body))
        return bool(data[0].get("blocked"))

    def _block(self, cid: str, blocked: bool) -> None:
        ref, mac = self._refs.get(cid), self._macs.get(cid)
        if not ref or not mac:
            raise WriteError("unifi.client_unknown")
        if self._blocked_now(ref["user"]) == blocked:
            return
        status, reply = self._call("POST", "api/s/{site}/cmd/stamgr", {"cmd": "block-sta" if blocked else "unblock-sta", "mac": mac})
        meta = reply.get("meta") if isinstance(reply, dict) else None
        if not 200 <= status < 300 or (isinstance(meta, dict) and meta.get("rc") not in (None, "ok")):
            raise WriteError("unifi.write_refused", _msg(reply))
        self.writes.append((ref["user"], ["block" if blocked else "unblock"]))
        if self._blocked_now(ref["user"]) != blocked:
            raise WriteError("unifi.not_applied")

    # ------------------------------------------------------------------ plan steps -> switch ports
    def _port_ref(self, pid: str) -> dict:
        ref = self._ports.get(pid)
        if not ref:
            raise WriteError("unifi.port_unknown")
        return ref

    def _fresh_port(self, pid: str):
        """The switch list read fresh; (classic reference, neutral state, neutral port)."""
        self._fresh("device")
        st = model.to_neutral(self._raw)
        port = next((p for p in st.get("ports") or [] if p.get("id") == pid), None)
        if port is None:
            raise WriteError("unifi.port_unknown")
        return self._port_ref(pid), st, port

    def _port_entry(self, ref: dict, after: dict) -> dict:
        """The override entry of one port with ``after`` applied (a copy; the port's own settings otherwise kept)."""
        idx = ref["idx"]
        entry = copy.deepcopy(ref["override"]) if isinstance(ref.get("override"), dict) else {"port_idx": idx}
        table = ref.get("table") or {}
        nets_raw = model.items(self._raw, "networkconf")
        if "name" in after:
            if after["name"] in ("", str(idx)):
                entry.pop("name", None)
            else:
                entry["name"] = after["name"]
        if "profile" in after:
            if after["profile"]:
                vid = self._profiles.get(after["profile"])
                if not vid:
                    raise WriteError("unifi.profile_unknown")
                entry["portconf_id"] = vid
            else:
                entry.pop("portconf_id", None)
        if "native_network" in after:
            net = self._net_ref(after["native_network"])
            if net["purpose"] not in LOCAL_PURPOSES:
                raise WriteError("unifi.network_not_local")
            entry["native_networkconf_id"] = net["id"]
        new_style = ref.get("new_style") or "tagged_vlan_mgmt" in entry or not ("forward" in entry or "forward" in table)
        conf = model.port_settings(table, entry, {}, nets_raw, model.default_network(nets_raw))
        mode = after.get("tagged_mode", conf["mode"])
        if "tagged_mode" in after or "tagged" in after:
            if mode == "custom" and not new_style:
                raise WriteError("unifi.field_not_supported")
            if new_style:
                entry["tagged_vlan_mgmt"] = TAGGED_OUT[mode]
            if mode == "custom":
                want = set()
                for nid in after.get("tagged", []):
                    ref_n = self._net_ref(nid)
                    want.add(ref_n["id"])
                native = entry.get("native_networkconf_id") or conf["native"]
                entry["excluded_networkconf_ids"] = [k for k in model.taggable(nets_raw) if k not in want and k != native]
        if ("forward" in entry or "forward" in table) and set(after) & {"native_network", "tagged_mode", "tagged"}:
            native = entry.get("native_networkconf_id") or conf["native"]
            if mode == "none":
                entry["forward"] = "native"                # the untagged network only
            elif mode == "all" and (not native or native == model.default_network(nets_raw)):
                entry["forward"] = "all"                   # the site's default: default network untagged, all tagged
            else:
                entry["forward"] = "customize"             # an untagged network of its own and/or a choice of tagged ones
        if "poe" in after:
            if not table.get("port_poe"):
                raise WriteError("unifi.no_poe")
            entry["poe_mode"] = "auto" if after["poe"] else "off"
        return entry

    def _overrides_with(self, ref: dict, entries: dict) -> list:
        """The switch's override list as read, with the entries of some ports replaced (port number -> entry, or None
        to remove it); checked: nothing but those entries differs, and in them only the fields this half may change."""
        fresh = [o for o in ref.get("overrides") or [] if isinstance(o, dict)]
        out, done = [], set()
        for o in fresh:
            idx = o.get("port_idx")
            if idx in entries:
                done.add(idx)
                if entries[idx] is not None:
                    out.append(entries[idx])
            else:
                out.append(copy.deepcopy(o))
        for idx, e in sorted(entries.items()):
            if idx not in done and e is not None:
                out.append(e)
        before = {o.get("port_idx"): o for o in fresh}
        after = {o.get("port_idx"): o for o in out}
        for idx in set(before) | set(after):
            b, a = before.get(idx), after.get(idx)
            if b == a:
                continue
            if idx not in entries:
                raise WriteError("unifi.body_check")
            if a is None:
                continue                                   # the way back removes an entry the plan added
            keys = set(b or {}) | set(a)
            changed = {k for k in keys if (b or {}).get(k, None) != a.get(k, None)} - {"port_idx"}
            if changed - PORT_WRITE_KEYS:                  # also for a port that had no entry before
                raise WriteError("unifi.body_check")
        return out

    def _put_overrides(self, device_id: str, overrides: list) -> None:
        rel = "api/s/{site}/rest/device/" + device_id
        status, reply = self._call("PUT", rel, {"port_overrides": overrides})
        meta = reply.get("meta") if isinstance(reply, dict) else None
        if not 200 <= status < 300 or (isinstance(meta, dict) and meta.get("rc") not in (None, "ok")):
            raise WriteError("unifi.write_refused", _msg(reply))
        self.writes.append((device_id, ["port_overrides"]))

    def _set_port(self, step: dict) -> None:
        ref, st, port = self._fresh_port(step["object"])
        if portconf.matches(st, port, step["after"]):
            return                                         # already as planned
        entry = self._port_entry(ref, step["after"])
        body = self._overrides_with(ref, {ref["idx"]: entry})
        self._put_overrides(ref["device"], body)
        ref, st, port = self._fresh_port(step["object"])
        if not portconf.matches(st, port, step["after"]):
            raise WriteError("unifi.not_applied")

    def _cycle_port(self, pid: str) -> None:
        ref = self._port_ref(pid)
        status, reply = self._call("POST", "api/s/{site}/cmd/devmgr", {"cmd": "power-cycle", "mac": ref["mac"], "port_idx": ref["idx"]})
        meta = reply.get("meta") if isinstance(reply, dict) else None
        if not 200 <= status < 300 or (isinstance(meta, dict) and meta.get("rc") not in (None, "ok")):
            raise WriteError("unifi.write_refused", _msg(reply))
        self.writes.append((pid, ["power-cycle"]))

    def _restore_ports(self, items: list) -> None:
        """Port overrides as saved, one write per switch (only the entries of the saved ports change)."""
        items = [i for i in items or [] if isinstance(i, dict) and _OID_RE.match(str(i.get("device") or "")) and _port_idx(i.get("idx"))]
        if not items:
            return
        self._fresh("device")
        by_dev = {}
        for i in items:
            by_dev.setdefault(i["device"], {})[i["idx"]] = copy.deepcopy(i.get("override")) if isinstance(i.get("override"), dict) else None
        for dev, entries in by_dev.items():
            ref = next((r for r in self._ports.values() if r["device"] == dev), None)
            if ref is None:
                raise WriteError("unifi.not_applied")
            now = {o.get("port_idx"): o for o in ref.get("overrides") or []}
            if all(now.get(idx) == e for idx, e in entries.items()):
                continue
            self._put_overrides(dev, self._overrides_with(ref, entries))

    def _port_lockout(self, state: dict, step: dict):
        """Family rules for ports: the port and its switch are known; power only on ports that deliver it and only in
        the automatic mode or off; profiles and networks the controller knows (tagged networks need a VLAN of their own);
        a choice of tagged networks only on releases that keep it per port."""
        ref = self._ports.get(step.get("object"))
        if not ref:
            return "fw.block.unknown"
        after = step.get("after") or {}
        table = ref.get("table") or {}
        if step.get("kind") == "port.cycle":
            return None if table.get("port_poe") else "pc.block.no_poe"
        if "poe" in after:
            mode = (ref.get("override") or {}).get("poe_mode", table.get("poe_mode"))
            if not table.get("port_poe"):
                return "pc.block.no_poe"
            if mode not in (None, "auto", "off"):
                return "pc.block.poe_mode"
        if after.get("profile") and after["profile"] not in self._profiles:
            return "fw.block.unknown"
        if "native_network" in after:
            net = self._nets.get(after["native_network"])
            if not net:
                return "fw.block.unknown"
            if net["purpose"] not in LOCAL_PURPOSES:
                return "fw.block.network_kind"
        taggable = set(model.taggable(model.items(self._raw, "networkconf")))
        for nid in after.get("tagged") or []:
            net = self._nets.get(nid)
            if not net or net["id"] not in taggable:
                return "pc.block.tagged_vlan"
        if after.get("tagged_mode") == "custom" and not ref.get("new_style") and \
                ("forward" in table or "forward" in (ref.get("override") or {})):
            return "fw.block.capability"
        return None

    # ------------------------------------------------------------------ plan steps -> zones and rules
    def _internal_zone(self):
        return next((k for k, r in self._zones.items() if r.get("key") == "internal"), None)

    def _zone_ref(self, zid: str) -> dict:
        ref = self._zones.get(zid)
        if not ref:
            raise WriteError("unifi.zone_unknown")
        return ref

    def _pol_ref(self, pid: str) -> dict:
        ref = self._pols.get(pid)
        if not ref:
            raise WriteError("unifi.rule_unknown")
        return ref

    def _v2(self, method: str, rel: str, body=None):
        """One write of the zone firewall API (plain JSON replies, no envelope)."""
        status, reply = self._call(method, rel, body)
        if not 200 <= status < 300:
            code = reply.get("code") if isinstance(reply, dict) else ""
            raise WriteError("unifi.write_refused", code if isinstance(code, str) and MSG_RE.match(code) else "")
        self.writes.append((rel.rsplit("/", 1)[-1], sorted(body) if isinstance(body, dict) else method))
        return reply if isinstance(reply, (dict, list)) else {}

    def _fresh_fw(self) -> None:
        self._fresh("zone")
        self._fresh("policies")

    def _pol_record(self, vid: str):
        status, body = self._call("GET", POLICY_AREA + "/" + vid)
        if status == 404:
            return None
        if status != 200 or not isinstance(body, dict):
            raise WriteError("unifi.read_failed")
        return body

    def _zone_now(self, vid: str):
        return next((r for r in self._zones.values() if r["id"] == vid), None)

    def _create_zone(self, step: dict) -> dict:
        name = step["after"]["name"]
        self._fresh("zone")
        for zid, ref in self._zones.items():               # already there (a repeated step): nothing to write
            if ref["record"].get("name") == name and self._own.has("zone", ref["id"]):
                self._created[step["object"]] = zid
                return {"id": zid}
        made = self._v2("POST", ZONE_AREA, {"name": name, "network_ids": []})
        vid = str(made.get("_id") or "") if isinstance(made, dict) else ""
        if not _OID_RE.match(vid):
            raise WriteError("unifi.write_refused", "no id")
        self._own.add("zone", vid)                         # remembered before anything else can go wrong
        self._fresh("zone")
        zid = next((k for k, r in self._zones.items() if r["id"] == vid), None)
        if zid is None or self._zones[zid]["record"].get("name") != name:
            raise WriteError("unifi.not_applied")
        self._created[step["object"]] = zid
        return {"id": zid}

    def _put_zone(self, ref: dict, name=None, network_ids=None) -> None:
        if ref.get("builtin"):
            raise WriteError("unifi.zone_builtin")
        rec = ref["record"]
        body = {"_id": ref["id"], "name": rec.get("name") if name is None else name,
                "network_ids": list(rec.get("network_ids") or []) if network_ids is None else list(network_ids)}
        if body["name"] == rec.get("name") and sorted(body["network_ids"]) == sorted(rec.get("network_ids") or []):
            return
        self._v2("PUT", ZONE_AREA + "/" + ref["id"], body)
        self._fresh("zone")
        now = self._zone_now(ref["id"])
        if now is None or now["record"].get("name") != body["name"] or \
                sorted(now["record"].get("network_ids") or []) != sorted(body["network_ids"]):
            raise WriteError("unifi.not_applied")

    def _move_network_zone(self, net_id: str, zone_id: str) -> None:
        """A network goes into a zone of its own (added to its list) or back into the internal zone (taken out of the
        list of the zone it is in); the controller keeps every network in exactly one zone."""
        vid = self._net_ref(net_id)["id"]
        self._fresh("zone")
        target = self._zone_ref(zone_id)
        current = next((r for r in self._zones.values() if vid in (r["record"].get("network_ids") or [])), None)
        if current is not None and current["id"] == target["id"]:
            return
        if target.get("key") == "internal":
            if current is None:
                return
            self._put_zone(current, network_ids=[n for n in current["record"].get("network_ids") or [] if n != vid])
        else:
            self._put_zone(target, network_ids=list(target["record"].get("network_ids") or []) + [vid])
        self._fresh("zone")
        now = next((r for r in self._zones.values() if vid in (r["record"].get("network_ids") or [])), None)
        if (now["id"] if now else None) != target["id"]:
            raise WriteError("unifi.not_applied")

    def _delete_zone(self, zid: str, force: bool = False) -> None:
        """Only a zone this write half created; refused while a network or a rule is in it (``force``, for the way
        back of a zone the plan itself created: take its networks out first; its rules are removed before)."""
        self._fresh_fw()
        ref = self._zone_ref(zid)
        if not self._own.has("zone", ref["id"]) or ref.get("builtin"):
            raise WriteError("unifi.not_own")
        if ref["record"].get("network_ids"):
            if not force:
                raise WriteError("unifi.zone_in_use")
            self._put_zone(ref, network_ids=[])
        for r in self._pols.values():
            rec = r["record"]
            if ref["id"] in ((rec.get("source") or {}).get("zone_id"), (rec.get("destination") or {}).get("zone_id")):
                raise WriteError("unifi.zone_in_use")      # the controller would delete the rule with the zone
        self._v2("DELETE", ZONE_AREA + "/" + ref["id"])
        self._fresh("zone")
        if self._zone_now(ref["id"]) is not None:
            raise WriteError("unifi.not_applied")
        self._own.drop("zone", ref["id"])

    def _side_out(self, side: dict, ports: str = "") -> dict:
        if not isinstance(side, dict) or side.get("group"):
            raise WriteError("unifi.field_not_supported")
        z = self._zone_ref(side.get("zone"))
        out = {"zone_id": z["id"], "matching_target": "ANY", "port_matching_type": "ANY", "match_opposite_ports": False}
        if side.get("net"):
            out.update(matching_target="NETWORK", network_ids=[self._net_ref(side["net"])["id"]], match_opposite_networks=False)
        if ports:
            out.update(port_matching_type="SPECIFIC", port=ports)
        return out

    def _policy_body(self, merged: dict, record: dict = None) -> dict:
        """The complete classic body of a rule from its neutral settings; fields Lanloom does not set keep the value
        the rule has (logging, address family)."""
        record = record or {}
        proto = PROTOCOLS_OUT.get(merged.get("proto") or "any")
        action = ACTIONS_OUT.get(merged.get("action"))
        ports = merged.get("ports") or ""
        if proto is None or action is None or (ports and proto == "all"):
            raise WriteError("unifi.field_not_supported")
        ipv = str(record.get("ip_version") or "BOTH").upper()
        return {"name": merged["name"], "enabled": bool(merged.get("enabled", True)), "action": action,
                "create_allow_respond": bool(merged.get("return_traffic")), "logging": bool(record.get("logging")),
                "protocol": proto, "ip_version": ipv if ipv in ("BOTH", "IPV4") else "BOTH", "connection_state_type": "ALL",
                "connection_states": [], "match_ip_sec": False, "match_opposite_protocol": False, "icmp_typename": "ANY",
                "icmp_v6_typename": "ANY", "schedule": {"mode": "ALWAYS"},
                "source": self._side_out(merged.get("src")), "destination": self._side_out(merged.get("dst"), ports)}

    @staticmethod
    def _pol_same(rec: dict, body: dict) -> bool:
        def side(x):
            x = x if isinstance(x, dict) else {}
            net = str(x.get("matching_target") or "ANY").upper() == "NETWORK"
            spec = str(x.get("port_matching_type") or "ANY").upper() == "SPECIFIC"
            return (x.get("zone_id"), tuple(x.get("network_ids") or []) if net else (), str(x.get("port") or "") if spec else "")
        if not isinstance(rec, dict):
            return False
        return all(rec.get(k) == body.get(k) for k in ("name", "enabled", "action", "create_allow_respond", "protocol")) and \
            side(rec.get("source")) == side(body.get("source")) and side(rec.get("destination")) == side(body.get("destination"))

    def _neutral_policy(self, pid: str) -> dict:
        return next((p for p in (self._state or {}).get("policies") or [] if p.get("id") == pid), None) or {}

    def _create_policy(self, step: dict) -> dict:
        body = self._policy_body(dict(zonefw.POLICY_DEFAULTS, **step["after"]))
        self._fresh("policies")
        for pid, ref in self._pols.items():                 # already there (a repeated step)
            if self._own.has("policy", ref["id"]) and self._pol_same(ref["record"], body):
                self._created[step["object"]] = pid
                return {"id": pid}
        made = self._v2("POST", POLICY_AREA, body)
        vid = str(made.get("_id") or "") if isinstance(made, dict) else ""
        if not _OID_RE.match(vid):
            raise WriteError("unifi.write_refused", "no id")
        self._own.add("policy", vid)
        rec = self._pol_record(vid)
        if rec is None or not self._pol_same(rec, body):
            raise WriteError("unifi.not_applied")
        self._fresh("policies")
        pid = model.oid("r", vid)
        self._created[step["object"]] = pid
        return {"id": pid}

    def _update_policy(self, step: dict) -> None:
        ref = self._pol_ref(step["object"])
        rec = self._pol_record(ref["id"])
        if rec is None:
            raise WriteError("unifi.not_applied")
        if not model.classic_editable(rec):
            raise WriteError("unifi.rule_not_editable")
        cur = self._neutral_policy(step["object"])
        merged = dict(zonefw.POLICY_DEFAULTS, **{k: cur[k] for k in ("name", "src", "dst", "action", "proto", "ports",
                                                                    "return_traffic", "enabled") if k in cur})
        merged.update(step["after"])
        body = self._policy_body(merged, rec)
        if self._pol_same(rec, body):
            return                                          # already as planned
        self._v2("PUT", POLICY_AREA + "/" + ref["id"], body)
        rec = self._pol_record(ref["id"])
        if rec is None or not self._pol_same(rec, body):
            raise WriteError("unifi.not_applied")

    def _delete_policy(self, pid: str) -> None:
        ref = self._pol_ref(pid)
        if not self._own.has("policy", ref["id"]):
            raise WriteError("unifi.not_own")               # nothing Lanloom did not create itself is ever deleted
        if self._pol_record(ref["id"]) is not None:
            self._v2("DELETE", POLICY_AREA + "/" + ref["id"])
            if self._pol_record(ref["id"]) is not None:
                raise WriteError("unifi.not_applied")
        self._own.drop("policy", ref["id"])

    def _pair_lists(self, st: dict, pair: tuple, order_of) -> tuple:
        """(before the built-in rules, after them) - the classic ids of a zone pair's own rules in the given order;
        each rule stays on its side of the pair's built-in rules."""
        group = [p for p in st.get("policies") or [] if zonefw.pair_of(p) == pair]
        pre = [p.get("order", 0) for p in group if p.get("builtin")]
        first = min(pre) if pre else None
        own = sorted((p for p in group if not p.get("builtin")), key=lambda p: (order_of(p), p["id"]))
        ids = lambda ps: [self._pols[p["id"]]["id"] for p in ps if p["id"] in self._pols]
        before = [p for p in own if first is None or p.get("order", 0) < first]
        return ids(before), ids([p for p in own if p not in before])

    def _put_order(self, pair: tuple, before: list, after: list) -> None:
        body = {"source_zone_id": self._zone_ref(pair[0])["id"], "destination_zone_id": self._zone_ref(pair[1])["id"],
                "before_predefined_ids": before, "after_predefined_ids": after}
        self._v2("PUT", POLICY_AREA + "/batch-reorder", body)

    def _reorder(self, pid: str, target: int) -> None:
        self._fresh_fw()
        st = model.to_neutral(self._raw)
        p = next((q for q in st.get("policies") or [] if q["id"] == pid), None)
        if p is None:
            raise WriteError("unifi.rule_unknown")
        if p.get("order") == target:
            return                                          # already as planned
        moved = zonefw.reorder(st, pid, target)
        if not moved:
            raise WriteError("unifi.order_range")
        pair = zonefw.pair_of(p)
        before, after = self._pair_lists(st, pair, lambda q: moved.get(q["id"], q.get("order", 0)))
        self._put_order(pair, before, after)
        self._fresh("policies")
        st = model.to_neutral(self._raw)
        now = next((q for q in st.get("policies") or [] if q["id"] == pid), None)
        if now is None or now.get("order") != target:
            raise WriteError("unifi.not_applied")

    def _restore_zones(self, items: list) -> None:
        """Zones as saved: names and the networks in them (a zone of its own the plan deleted comes back)."""
        if not items:
            return
        self._fresh_fw()
        nets = {r["id"] for r in self._nets.values()}
        for item in items:
            if not isinstance(item, dict) or item.get("builtin") or not _OID_RE.match(str(item.get("ref") or "")):
                continue
            saved = item.get("record") or {}
            keep = [n for n in saved.get("network_ids") or [] if n in nets]
            now = self._zone_now(item["ref"])
            if now is not None:
                self._put_zone(now, name=saved.get("name"), network_ids=keep)
            elif item.get("own") and _short_name(saved.get("name") or ""):
                made = self._v2("POST", ZONE_AREA, {"name": saved["name"], "network_ids": keep})
                if isinstance(made, dict) and _OID_RE.match(str(made.get("_id") or "")):
                    self._own.add("zone", str(made["_id"]))
                self._fresh("zone")

    def _restore_policies(self, items: list) -> None:
        """Rules as saved (settings, then the order of each zone pair); an own rule the plan deleted comes back."""
        if not items:
            return
        self._fresh_fw()
        renamed = {}
        pairs = {}
        for item in items:
            if not isinstance(item, dict) or not _OID_RE.match(str(item.get("ref") or "")):
                continue
            saved = item.get("record") or {}
            if saved.get("predefined") or not model.classic_editable(saved):
                continue
            body = {k: saved[k] for k in POLICY_VALUES if k in saved}
            if set(body) != set(POLICY_VALUES):
                continue
            now = self._pol_record(item["ref"])
            if now is not None:
                if not self._pol_same(now, body):
                    self._v2("PUT", POLICY_AREA + "/" + item["ref"], body)
                ref_id = item["ref"]
            elif item.get("own"):
                made = self._v2("POST", POLICY_AREA, body)
                ref_id = str(made.get("_id") or "") if isinstance(made, dict) else ""
                if not _OID_RE.match(ref_id):
                    continue
                self._own.add("policy", ref_id)
                renamed[item["ref"]] = ref_id
            else:
                continue
            key = ((saved.get("source") or {}).get("zone_id"), (saved.get("destination") or {}).get("zone_id"))
            pairs.setdefault(key, []).append((saved.get("index") or 0, ref_id))
        self._fresh("policies")
        current = {}
        for r in self._pols.values():
            rec = r["record"]
            if rec.get("predefined"):
                continue
            key = ((rec.get("source") or {}).get("zone_id"), (rec.get("destination") or {}).get("zone_id"))
            current.setdefault(key, []).append((rec.get("index") or 0, r["id"]))
        zone_by_vid = {r["id"]: k for k, r in self._zones.items()}
        for key, saved_list in pairs.items():
            want = [vid for _, vid in sorted(saved_list)]
            have = [vid for _, vid in sorted(current.get(key) or []) if vid in want]
            if have == want or key[0] not in zone_by_vid or key[1] not in zone_by_vid:
                continue
            extra = [vid for _, vid in sorted(current.get(key) or []) if vid not in want]
            self._put_order((zone_by_vid[key[0]], zone_by_vid[key[1]]), want + extra, [])

    def _restore_body(self, saved: dict, now: dict) -> dict:
        body = {}
        if saved.get("name", "") != now.get("name", ""):
            body["name"] = saved.get("name") or ""
        if saved.get("use_fixedip"):
            for k in ("use_fixedip", "fixed_ip", "network_id"):
                if saved.get(k) not in (None, "") and saved.get(k) != now.get(k):
                    body[k] = saved[k]
            if body.keys() & {"fixed_ip", "network_id"}:
                body["use_fixedip"] = True
        elif now.get("use_fixedip"):
            body["use_fixedip"] = False
        if saved.get("virtual_network_override_enabled"):
            if not now.get("virtual_network_override_enabled") or now.get("virtual_network_override_id") != saved.get("virtual_network_override_id"):
                body["virtual_network_override_enabled"] = True
                if _oid_or_empty(saved.get("virtual_network_override_id")) and saved.get("virtual_network_override_id"):
                    body["virtual_network_override_id"] = saved["virtual_network_override_id"]
        elif now.get("virtual_network_override_enabled"):
            body["virtual_network_override_enabled"] = False
        return body

    def restore(self, snapshot: dict) -> None:
        """Put everything a plan touched back as saved: first remove what it created (port forwards, fixed addresses,
        then networks - each only when this write half created it), then the client settings, the port forwards and the
        networks as they were (a network or forward the plan deleted is created again)."""
        snapshot = snapshot or {}
        created = [c for c in snapshot.get("created") or [] if isinstance(c, str)]
        if created and ("zones" in snapshot or "policies" in snapshot):
            self._fresh_fw()
            for cid in created:                             # rules first: a zone is removed only once nothing names it
                if cid in self._pols and self._own.has("policy", self._pols[cid]["id"]):
                    self._delete_policy(cid)
            self._fresh_fw()
            for cid in created:
                if cid in self._zones and self._own.has("zone", self._zones[cid]["id"]):
                    self._delete_zone(cid, force=True)
        if created:
            self._fresh("networkconf")
            self._fresh("portforward")
            self._fresh("user")
            res_ids = {r["reservation"]: r for r in self._refs.values() if r.get("reservation")}
            for cid in created:
                if cid in self._fwds:
                    self._delete("portforward", self._fwds[cid]["id"])
            for cid in created:
                if cid in res_ids:
                    uid = res_ids[cid]["user"]
                    if self._user_record(uid).get("use_fixedip"):
                        self._put(uid, {"use_fixedip": False})
            for cid in created:
                if cid in self._nets:
                    self._delete("networkconf", self._nets[cid]["id"])
            self._prune_own()
        if created or snapshot.get("ssids"):
            self._fresh("wlanconf")
            for cid in created:
                if cid in self._ssids and self._own.has("wlanconf", self._ssids[cid]["id"]):
                    self._delete("wlanconf", self._ssids[cid]["id"])
        for item in snapshot.get("ssids") or []:
            self._restore_ssid(item)
        self._restore_clients(list(snapshot.get("devices") or []) + list(snapshot.get("dhcp_reservations") or []))
        for item in snapshot.get("port_forwards") or []:
            self._restore_object("portforward", item, model.FORWARD_FIELDS, FWD_UPDATE_VALUES, FWD_VALUES)
        for item in snapshot.get("networks") or []:
            self._restore_object("networkconf", item, model.NET_FIELDS, NET_UPDATE_VALUES, NET_CREATE_VALUES)
        if snapshot.get("zones") or snapshot.get("policies"):
            self._fresh("networkconf")
            self._restore_zones(snapshot.get("zones"))
            self._restore_policies(snapshot.get("policies"))
        if snapshot.get("ports"):
            self._restore_ports(snapshot.get("ports"))

    def _restore_ssid(self, item: dict) -> None:
        """An SSID as saved: its settings written back (never its password); one this write half created and the plan
        deleted is created again (with a new random password)."""
        if not isinstance(item, dict) or not _OID_RE.match(str(item.get("ref") or "")):
            return
        saved = item.get("record") or {}
        plain = ("name", "networkconf_id", "wlan_bands", "security", "wpa_mode", "wpa3_support", "wpa3_transition",
                 "hide_ssid", "enabled")
        rec = self._record("wlanconf", item["ref"])
        if rec is not None:
            body = {k: saved[k] for k in plain if k in saved and saved[k] is not None and rec.get(k) != saved[k]
                    and self._value_ok(WLAN_UPDATE_VALUES[k], saved[k])}
            if body.get("security", rec.get("security")) == "wpapsk" and not rec.get("x_passphrase"):
                body["x_passphrase"] = self._new_password()
            if body:
                self._write("PUT", "wlanconf", item["ref"], body)
            return
        if not item.get("own"):
            raise WriteError("unifi.not_applied")         # only what Lanloom created could have been deleted
        body = {k: saved[k] for k in plain + ("ap_group_ids", "ap_group_mode", "usergroup_id")
                if k in saved and saved[k] is not None and self._value_ok(WLAN_CREATE_VALUES[k], saved[k])}
        if body.get("security") == "wpapsk":
            body.update(x_passphrase=self._new_password(), wpa_enc="ccmp")
        made = self._write("POST", "wlanconf", body=body)
        if _OID_RE.match(str(made.get("_id") or "")):
            self._own.add("wlanconf", str(made["_id"]))

    @staticmethod
    def _value_ok(check, value) -> bool:
        try:
            check(value)
            return True
        except (ValueError, TypeError):
            return False

    def _prune_own(self) -> None:
        """Forget own objects that no longer exist on the controller (removed there by hand)."""
        present = {"networkconf": {r["id"] for r in self._nets.values()}, "portforward": {r["id"] for r in self._fwds.values()}}
        for area, ids in present.items():
            for vid in list(self._own.data[area]):
                if vid not in ids:
                    self._own.drop(area, vid)

    def _restore_object(self, area: str, item: dict, fields, update_values: dict, create_values: dict) -> None:
        if not isinstance(item, dict) or not _OID_RE.match(str(item.get("ref") or "")):
            return
        saved = item.get("record") or {}
        rec = self._record(area, item["ref"])
        if rec is not None:
            body = {k: saved[k] for k in update_values if k in saved and rec.get(k) != saved[k]}
            if body:
                self._write("PUT", area, item["ref"], body)
            return
        if not item.get("own"):
            raise WriteError("unifi.not_applied")         # only what Lanloom created could have been deleted
        body = {k: saved.get(k) for k in create_values}
        if area == "networkconf":
            body.update(purpose="corporate", networkgroup="LAN", vlan_enabled=True, enabled=True, setting_preference="manual",
                        internet_access_enabled=True, ipv6_interface_type="none")
            if isinstance(body.get("vlan"), str) and body["vlan"].isdigit():
                body["vlan"] = int(body["vlan"])
        else:
            body.update(src="any", pfwd_interface="wan", log=bool(saved.get("log")))
        made = self._write("POST", area, body=body)
        if _OID_RE.match(str(made.get("_id") or "")):
            self._own.add(area, str(made["_id"]))

    def _restore_clients(self, items: list) -> None:
        """Clients that give up a fixed address go first, so two clients that swapped addresses never collide; a
        failure is retried once after the others."""
        items = [d for d in items if isinstance(d, dict) and _OID_RE.match(str(d.get("user") or ""))]
        todo = []
        for d in items:
            now = self._user_record(d["user"])
            body = self._restore_body(d.get("record") or {}, now)
            if body:
                todo.append((d["user"], body, bool(now.get("use_fixedip")) and not body.get("use_fixedip", True)))
        todo.sort(key=lambda x: not x[2])
        failed = []
        for uid, body, _ in todo:
            try:
                self._put(uid, body)
            except WriteError:
                failed.append((uid, body))
        for uid, body in failed:
            self._put(uid, body)
        back = [d for d in items if isinstance(d.get("blocked"), bool) and self._blocked_now(d["user"]) != d["blocked"]]
        if back:                                         # a block the plan set or lifted goes back as it was
            if any(d.get("id") not in self._macs for d in back):
                self._fresh("user")
            for d in back:
                if d.get("id") not in self._refs or d.get("id") not in self._macs:
                    raise WriteError("unifi.client_unknown")
                self._block(d["id"], d["blocked"])

    # ------------------------------------------------------------------ after the change
    def settle(self, steps: list) -> None:
        wait = 0
        for st in steps:
            ref = self._refs.get(stepkinds.step_object(st)) or {}
            if st["kind"] not in ("move", "client.set"):
                wait = max(wait, min(self.settle_s, SETTLE_CONFIG_S) if st["kind"].startswith(("network.", "forward.", "zone.", "policy.", "port.", "ssid."))
                           else SETTLE_ADDRESS_S if self.settle_s else 0)
            elif st["kind"] == "move" or "network" in st["after"]:
                if ref.get("online") and not ref.get("wired"):
                    wait = max(wait, self.settle_s)
            elif "fixed_ip" in st["after"]:
                wait = max(wait, SETTLE_ADDRESS_S)
        if wait:
            self.sleep(wait)

    def probe(self, steps: list) -> dict:
        """Watch: every client still carries the settings of the plan; a moved client that is online sits in its new
        network. A wireless client that is offline right now is not counted (it rejoins when it wakes up). Two failed
        reads in a row of the controller itself count as a global failure."""
        try:
            sta = self._get("sta") or {}
        except WriteError:
            self._probe_fail += 1
            return {"global_ok": self._probe_fail < 2, "missing": []}
        self._probe_fail = 0
        online = {}
        for c in model.items({"sta": sta}, "sta"):
            mac = model.norm_mac(c.get("mac"))
            if mac:
                online[model.oid("c", mac)] = c
        missing = []
        others = [st for st in steps if st["kind"] not in ("move", "client.set")]
        if others:
            try:
                missing.extend(self._probe_config(others))
            except WriteError:
                self._probe_fail += 1
                return {"global_ok": self._probe_fail < 2, "missing": []}
        for st in steps:
            if st["kind"] not in ("move", "client.set"):
                continue
            oid = stepkinds.step_object(st)
            try:
                uid, body = self.body_for(st)
                if not self._matches(self._user_record(uid), body):
                    missing.append(oid)
                    continue
            except WriteError:
                missing.append(oid)
                continue
            if "virtual_network_override_id" in body and oid in online:
                now_net = online[oid].get("network_id")
                if now_net and now_net != body["virtual_network_override_id"]:
                    missing.append(oid)
        return {"global_ok": True, "missing": missing}

    def _probe_config(self, steps: list) -> list:
        """Ids of gateway objects that are not as planned (a created object under the id the controller gave it)."""
        self._fresh("networkconf")
        self._fresh("portforward")
        self._fresh("user")
        fw = None
        if any(st["kind"] in zonefw.ZONE_KINDS or "zone" in st["after"] for st in steps):
            self._fresh_fw()
            fw = model.to_neutral(self._raw)
        res_ids = {r["reservation"]: r for r in self._refs.values() if r.get("reservation")}
        ports_now = None
        if any(st["kind"] == "port.set" for st in steps):
            self._fresh("device")
            ports_now = model.to_neutral(self._raw)
        wireless_now = None
        if any(st["kind"] in wireless.SSID_KINDS for st in steps):
            self._fresh("wlanconf")
            wireless_now = model.to_neutral(self._raw)
        missing = []
        for st in steps:
            kind, after = st["kind"], st["after"]
            oid = self._created.get(st["object"], st["object"])
            ok = True
            if kind == "port.cycle":
                continue                                # a one-off command: nothing to keep
            if kind in wireless.SSID_KINDS:
                obj = next((w for w in wireless_now.get("ssids") or [] if w.get("id") == oid), None)
                if (obj is not None) if kind == "ssid.delete" else (obj is None or not wireless.matches(obj, after)):
                    missing.append(oid)
                continue
            if kind == "client.block":
                ref = self._refs.get(oid)
                if not ref or self._blocked_now(ref["user"]) != bool(after["blocked"]):
                    missing.append(oid)
                continue
            if kind == "port.set":
                port = next((p for p in ports_now.get("ports") or [] if p.get("id") == oid), None)
                if port is None or not portconf.matches(ports_now, port, after):
                    missing.append(oid)
                continue
            if fw is not None and (kind in zonefw.ZONE_KINDS or (kind.startswith("network.") and "zone" in after)):
                area = "zones" if kind.startswith("zone.") else "policies" if kind.startswith("policy.") else "networks"
                obj = next((o for o in fw.get(area) or [] if o.get("id") == oid), None)
                if kind.endswith(".delete"):
                    ok = obj is None
                elif obj is None:
                    ok = False
                elif kind.startswith("network."):
                    ok = obj.get("zone") == after["zone"]
                else:
                    ok = all(obj.get(f) == v for f, v in after.items() if f in ("name", "action", "proto", "ports", "enabled",
                                                                               "return_traffic", "src", "dst", "order"))
                if not ok:
                    missing.append(oid)
                if not ok or not kind.startswith("network."):
                    continue
            if kind == "network.create":
                ref = self._nets.get(oid)
                ok = bool(ref) and (ref.get("record") or {}).get("name") == after["name"] and ref.get("vlan") == after["vlan"]
            elif kind == "network.update":
                ref = self._nets.get(oid)
                ok = bool(ref) and self._matches(ref.get("record") or {}, self._network_update(after))
            elif kind in ("network.delete", "forward.delete"):
                ok = oid not in (self._nets if kind == "network.delete" else self._fwds)
            elif kind == "forward.create":
                ref = self._fwds.get(oid)
                ok = bool(ref) and self._matches(ref.get("record") or {}, {k: v for k, v in self._forward_body(after, True).items()
                                                                           if k in ("name", "dst_port", "fwd", "proto", "enabled")})
            elif kind == "forward.update":
                ref = self._fwds.get(oid)
                ok = bool(ref) and self._matches(ref.get("record") or {}, self._forward_body(after, False))
            elif kind == "dhcp.reserve":
                ref = res_ids.get(oid)
                rec = self._user_record(ref["user"]) if ref else {}
                ok = bool(rec.get("use_fixedip")) and rec.get("fixed_ip") == after["ip"]
            elif kind == "dhcp.release":
                ref = res_ids.get(oid)
                ok = not ref or not self._user_record(ref["user"]).get("use_fixedip")
            if not ok:
                missing.append(oid)
        return missing

    def _controller_ip(self):
        from ...connguard import first_address
        return first_address(self._host, self._port)

    def self_address(self):
        """The address this executor reaches the controller from (see ``connguard.source_address``; nothing is sent)."""
        from ...connguard import source_address
        ip = self._controller_ip()
        return source_address(ip, self._port) if ip else None

    def plan_lockout(self, state: dict, steps: list):
        """The gateway rules over the whole plan, then the zone firewall: rule order, and this executor keeps its way
        to the controller."""
        why = netconf.plan_lockout(state, steps) or portconf.plan_lockout(state, steps) or wireless.plan_lockout(state, steps)
        if why:
            return why
        if not any(st.get("kind") in zonefw.ZONE_KINDS or "zone" in (st.get("after") or {}) for st in steps):
            return None
        ctl = self._controller_ip()
        me = self.self_address()
        why, _ = zonefw.plan_lockout_vars(state, steps, sources=[me] if me else [],
                                          targets=[(ctl, self._port, "zf.path.controller")] if ctl else [])
        return why

    def _wireless_lockout(self, state: dict, step: dict):
        """Family rules for SSIDs and blocks: the SSID and the client are known to the controller; delete only an SSID this
        write half created; never the security of an enterprise SSID, never the network or security of one with private
        pre-shared keys; the network of an SSID is a local one."""
        kind, after = step.get("kind"), step.get("after") or {}
        if kind == "client.block":
            oid = step.get("object")
            return None if oid in self._refs and oid in self._macs else "fw.block.unknown"
        if kind == "ssid.create":
            net = self._nets.get(after.get("network"))
            return None if net and net["purpose"] in LOCAL_PURPOSES else "fw.block.network_kind" if net else "fw.block.unknown"
        ref = self._ssids.get(step.get("object"))
        if not ref:
            return "fw.block.unknown"
        rec = ref["record"]
        if kind == "ssid.delete":
            return None if self._own.has("wlanconf", ref["id"]) else "fw.block.not_own"
        if "security" in after and rec.get("security") not in ("open", "wpapsk"):
            return "ws.block.enterprise"
        if rec.get("private_preshared_keys_enabled") and ("security" in after or "network" in after):
            return "ws.block.private_keys"
        if "network" in after:
            net = self._nets.get(after["network"])
            if not net:
                return "fw.block.unknown"
            if net["purpose"] not in LOCAL_PURPOSES:
                return "fw.block.network_kind"
        return None

    def _zone_lockout(self, state: dict, step: dict):
        """Family rules for zones and rules: names only for zones; no address groups, no ICMP rules; only rules whose
        every setting can be carried over; delete only own zones and rules; reorder within one side of the pair's
        built-in rules."""
        kind, after = step.get("kind"), step.get("after") or {}
        obj = step.get("object")
        if kind in ("zone.create", "zone.update") and set(after) - {"name"}:
            return "fw.block.capability"
        if kind == "zone.delete":
            ref = self._zones.get(obj) or {}
            return None if self._own.has("zone", ref.get("id", "")) else "fw.block.not_own"
        if kind in ("policy.create", "policy.update"):
            old = next((p for p in state.get("policies") or [] if p.get("id") == obj), None) or {}
            merged = dict(zonefw.POLICY_DEFAULTS, **dict(old, **after))
            if merged.get("proto") not in PROTOCOLS_OUT:
                return "fw.block.capability"
            for side in ("src", "dst"):
                sd = merged.get(side) or {}
                if sd.get("group") or sd.get("match") or (side == "src" and sd.get("ports")):
                    return "fw.block.capability"
                if sd.get("zone") not in self._zones:
                    return "fw.block.unknown"
            if kind == "policy.update":
                ref = self._pols.get(obj)
                if not ref or not model.classic_editable(ref["record"]):
                    return "fw.block.builtin"
            return None
        if kind == "policy.delete":
            ref = self._pols.get(obj) or {}
            return None if self._own.has("policy", ref.get("id", "")) else "fw.block.not_own"
        if kind == "policy.reorder":
            p = next((q for q in state.get("policies") or [] if q.get("id") == obj), None)
            if not p:
                return "fw.block.unknown"
            holder = next((q for q in state.get("policies") or [] if zonefw.pair_of(q) == zonefw.pair_of(p)
                           and not q.get("builtin") and q.get("order") == after.get("order")), None)
            pre = [q.get("order", 0) for q in state.get("policies") or [] if zonefw.pair_of(q) == zonefw.pair_of(p) and q.get("builtin")]
            if holder is None:
                return "zf.block.order_range"
            if pre and (p.get("order", 0) < min(pre)) != (holder.get("order", 0) < min(pre)):
                return "zf.block.order_range"
            return None
        return None

    def lockout(self, state: dict, step: dict):
        """Family rules on top of the neutral ones: a wired client changes its network through its switch port (port
        settings come with a later stage), and only local networks take clients; the gateway rules of
        ``firewall.netconf`` with the controller's own address; VLANs up to 4009; a network goes into a zone of its
        own or the internal zone, never into another system zone; only networks, forwards, zones and rules this write
        half created may be deleted; the zone and rule rules of ``_zone_lockout``."""
        kind = step.get("kind")
        if kind in zonefw.ZONE_KINDS:
            return self._zone_lockout(state, step)
        if kind in portconf.PORT_KINDS:
            guard = [self._host]
            try:
                guard += [a for a in (self._controller_ip(), self.self_address()) if a]
            except OSError:
                pass
            return portconf.step_lockout(state, step, {"addresses": guard}) or self._port_lockout(state, step)
        if kind in netconf.NET_KINDS:
            why = netconf.step_lockout(state, step, critical=[self._host])
            if why:
                return why
            after = step.get("after") or {}
            if after.get("zone") is not None and kind in ("network.create", "network.update"):
                ref = self._zones.get(after["zone"]) or {}
                if not ref or (ref.get("builtin") and ref.get("key") != "internal"):
                    return "zf.block.zone_system"
            if kind == "network.create":
                if isinstance(after.get("vlan"), int) and after["vlan"] > VLAN_MAX:
                    return "fw.block.net_vlan"
            if kind == "network.update":
                ref = self._nets.get(step.get("object")) or {}
                if ref.get("purpose") not in LOCAL_PURPOSES:
                    return "fw.block.network_kind"
                if set(after) - {"name", "dhcp", "zone"}:
                    return "fw.block.capability"
            if kind in ("network.delete", "forward.delete"):
                refs = self._nets if kind == "network.delete" else self._fwds
                ref = refs.get(step.get("object")) or {}
                if not self._own.has("networkconf" if kind == "network.delete" else "portforward", ref.get("id", "")):
                    return "fw.block.not_own"
            return None
        if kind in wireless.WIRELESS_KINDS:
            guard = [self._host]
            try:
                guard += [a for a in (self._controller_ip(), self.self_address()) if a]
            except OSError:
                pass
            return wireless.step_lockout(state, step, {"addresses": guard}) or self._wireless_lockout(state, step)
        if kind not in ("move", "client.set"):
            return "fw.block.capability"
        ref = self._refs.get(stepkinds.step_object(step)) or {}
        after = step.get("after") or {}
        if step["kind"] == "move" or "network" in after:
            if ref.get("wired"):
                return "fw.block.wired_move"
            net = self._nets.get(after.get("network"))
            if net and net["purpose"] not in LOCAL_PURPOSES:
                return "fw.block.network_kind"
        return None
