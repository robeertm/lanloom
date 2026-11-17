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
"""
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
from ...firewall import netconf
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
READ_PATHS = (
    _P + r"/api/s/%s/(?:self|stat/(?:sta|device|sysinfo)|rest/(?:networkconf|user|portforward|wlanconf|portconf))" % _S,
    _P + r"/api/s/%s/rest/(?:user|networkconf|portforward)/%s" % (_S, OID),
    _P + r"/v2/api/site/%s/(?:firewall/zone|firewall-policies)" % _S,
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
       Rule("DELETE", _P + r"/api/s/%s/rest/portforward/%s" % (_S, OID))])
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


class OwnLedger:
    """Classic ids of the networks and port forwards this write half created (``unifi-own.json``, 0600, never through
    a symbolic link). Only those may ever be deleted - by a plan or by the way back."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.data = {"networkconf": [], "portforward": []}
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
        role = "superadmin" if me.get("is_super") else ("owner" if me.get("is_owner") else str(me.get("site_role") or ""))
        ok, warnings = vet_write_account({"principal": self._user, "role": role})
        if role == "readonly":
            return False, ["read_only"]
        return ok, warnings

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
        if after.get("zone") not in (None, self._default_zone()):
            raise WriteError("unifi.zone_later")         # zones of its own come with the zone firewall stage
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
        if set(after) - {"name", "dhcp"}:
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
        for oid in ids:
            if stepkinds.NEW_ID.match(str(oid)):
                continue
            if oid in self._refs:
                ref = self._refs[oid]
                out["devices"].append({"id": oid, "user": ref["user"], "record": self._user_record(ref["user"])})
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
        return {k: v for k, v in out.items() if v or k == "devices"}

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
        if kind == "network.update":
            ref = self._net_ref(step["object"])
            if ref["purpose"] not in LOCAL_PURPOSES:
                raise WriteError("unifi.network_not_local")
            self._update("networkconf", ref["id"], self._network_update(step["after"]))
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
        raise WriteError("unifi.kind_not_supported")

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
        self._restore_clients(list(snapshot.get("devices") or []) + list(snapshot.get("dhcp_reservations") or []))
        for item in snapshot.get("port_forwards") or []:
            self._restore_object("portforward", item, model.FORWARD_FIELDS, FWD_UPDATE_VALUES, FWD_VALUES)
        for item in snapshot.get("networks") or []:
            self._restore_object("networkconf", item, model.NET_FIELDS, NET_UPDATE_VALUES, NET_CREATE_VALUES)

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

    # ------------------------------------------------------------------ after the change
    def settle(self, steps: list) -> None:
        wait = 0
        for st in steps:
            ref = self._refs.get(stepkinds.step_object(st)) or {}
            if st["kind"] not in ("move", "client.set"):
                wait = max(wait, min(self.settle_s, SETTLE_CONFIG_S) if st["kind"].startswith(("network.", "forward."))
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
        res_ids = {r["reservation"]: r for r in self._refs.values() if r.get("reservation")}
        missing = []
        for st in steps:
            kind, after = st["kind"], st["after"]
            oid = self._created.get(st["object"], st["object"])
            ok = True
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

    def plan_lockout(self, state: dict, steps: list):
        return netconf.plan_lockout(state, steps)

    def lockout(self, state: dict, step: dict):
        """Family rules on top of the neutral ones: a wired client changes its network through its switch port (port
        settings come with a later stage), and only local networks take clients; the gateway rules of
        ``firewall.netconf`` with the controller's own address; VLANs up to 4009; new networks land in the internal
        zone (zones of their own come with the zone firewall stage); only networks and forwards this write half
        created may be deleted."""
        kind = step.get("kind")
        if kind in netconf.NET_KINDS:
            why = netconf.step_lockout(state, step, critical=[self._host])
            if why:
                return why
            after = step.get("after") or {}
            if kind == "network.create":
                if isinstance(after.get("vlan"), int) and after["vlan"] > VLAN_MAX:
                    return "fw.block.net_vlan"
                if after.get("zone") not in (None, netconf.default_zone(state)):
                    return "fw.block.zone_later"
            if kind == "network.update":
                ref = self._nets.get(step.get("object")) or {}
                if ref.get("purpose") not in LOCAL_PURPOSES:
                    return "fw.block.network_kind"
                if set(after) - {"name", "dhcp"}:
                    return "fw.block.capability"
            if kind in ("network.delete", "forward.delete"):
                refs = self._nets if kind == "network.delete" else self._fwds
                ref = refs.get(step.get("object")) or {}
                if not self._own.has("networkconf" if kind == "network.delete" else "portforward", ref.get("id", "")):
                    return "fw.block.not_own"
            return None
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
