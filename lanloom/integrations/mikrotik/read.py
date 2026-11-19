"""Read half of the MikroTik family: reads the router through its REST API, never writes.

Every request goes through ``transport.Transport`` with a *read* allow-list: GET on the fixed paths below and nothing
else - no POST at all (the API needs no sign-in call: user name and password travel as Basic authentication on every
request), so no change, no command, no reboot, no package action can leave this process. The REST interface of the
router serves changes and commands on the same paths through POST, PUT, PATCH and DELETE; the allow-list refuses
every one of them before anything is sent.

Cadence: the light poll (state of interfaces, routes, clients, neighbours, wireless registrations) every 60 s, the
full poll (adds addresses, DHCP servers, bridge settings, VLAN table, firewall and NAT rules) every 5 minutes. A path
the router does not have (an older release, a package that is not installed) or the user may not read is left out
from then on for an hour; the other areas still show. A refused sign-in (HTTP 401) pauses the half for 15 minutes, so a
wrong password never floods the router's log with failed logins.

Events:

* ``{"type": "topology", "source": "controller", ...}`` - same shape as the generic controller source, so the topology
  resolver merges it with manual links, LLDP and address tables;
* ``{"type": "config", "source": "family", "family": "mikrotik", "state": <neutral model v2>, "capabilities": ...}`` -
  what the firewall tab and the inventory show (read only).

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
import asyncio
import base64
import hashlib
import json
import re
import threading
import time

from . import CAPABILITIES, model
from .. import capabilities
from ..credentials import Secret
from ..transport import Forbidden, Transport, TransportError, read_allowlist
from ...sources._common import SourceError, StatusTracker, emit_event, now, run_with_backoff, short_test

MODULE = "family"
SOURCE = "family"
URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}(?::\d{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](?::\d{1,5})?)/?\Z", re.I)
USER_RE = re.compile(r"\A[A-Za-z0-9_.@-]{1,64}\Z")
LIGHT_S = 60.0
FULL_S = 300.0
PAUSE_S = 900.0                  # after a refused sign-in
SKIP_S = 3600.0                  # a path that is missing or not allowed is asked again after this
MAX_BYTES_ITEMS = model.MAX_ITEMS

# key -> path; every one optional (the area simply stays empty). LIGHT keys are read on every poll.
LIGHT = {
    "resource": "/rest/system/resource",
    "interfaces": "/rest/interface",
    "ethernet": "/rest/interface/ethernet",
    "poe": "/rest/interface/ethernet/poe",
    "routes": "/rest/ip/route",
    "dhcp_clients": "/rest/ip/dhcp-client",
    "pppoe_clients": "/rest/interface/pppoe-client",
    "leases": "/rest/ip/dhcp-server/lease",
    "arp": "/rest/ip/arp",
    "bridge_hosts": "/rest/interface/bridge/host",
    "neighbors": "/rest/ip/neighbor",
    "wifi_reg": "/rest/interface/%s/registration-table" % model.WIRELESS_PKG,
    "wireless_reg": "/rest/interface/wireless/registration-table",
}
FULL = {
    "identity": "/rest/system/identity",
    "routerboard": "/rest/system/routerboard",
    "bridges": "/rest/interface/bridge",
    "bridge_ports": "/rest/interface/bridge/port",
    "bridge_vlans": "/rest/interface/bridge/vlan",
    "vlans": "/rest/interface/vlan",
    "addresses": "/rest/ip/address",
    "pools": "/rest/ip/pool",
    "dhcp_servers": "/rest/ip/dhcp-server",
    "filter": "/rest/ip/firewall/filter",
    "nat": "/rest/ip/firewall/nat",
}
SINGLE = ("resource", "identity", "routerboard")          # replies that are one object, not a list
ACCOUNT_PATHS = {"users": "/rest/user", "groups": "/rest/user/group"}
ALLOW = read_allowlist(tuple(re.escape(p) for p in list(LIGHT.values()) + list(FULL.values()) + list(ACCOUNT_PATHS.values())))
# reply keys that carry secrets in some releases (never kept, not even in the collected raw dict)
SECRET_KEYS = re.compile(r"(?i)(password|passwd|secret|psk|pre-shared|private|key$|token|certificate|\bcert\b)")
# group policies that let an account change something (the router lists them as ``read,write,policy,...``; ``!name``
# marks a policy that is switched off). The router's own ``read`` group carries test, password, sniff, sensitive and
# romon as well: they run diagnostics, change the user's own password or show secrets, none of them changes the
# configuration, so they do not make an account write-capable (``ftp`` does: it reaches the router's files).
WRITE_POLICIES = frozenset({"write", "policy", "reboot", "ftp"})

_LOCK = threading.Lock()
_PAUSED = {}                     # key of host+user+password -> monotonic time until which sign-in is not tried


def _secret(value) -> Secret:
    return value if isinstance(value, Secret) else Secret(value)


def scrub(obj, depth=0):
    """A copy of a reply without any field whose name says it carries a password, key or certificate."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        return {k: scrub(v, depth + 1) for k, v in obj.items() if isinstance(v, (dict, list)) or not SECRET_KEYS.search(str(k))}
    if isinstance(obj, list):
        return [scrub(v, depth + 1) for v in obj[:MAX_BYTES_ITEMS]]
    return obj


class MikrotikReader:
    family = "mikrotik"
    name = SOURCE

    def __init__(self, url=None, username=None, password=None, allow_self_signed=False, interval=LIGHT_S,
                 full_interval=FULL_S, timeout=15.0):
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise SourceError("source.family.config", "address missing or invalid")
        self._base = "%s://%s" % (m.group(1).lower(), m.group(2))
        self._host = m.group(2)
        self._user = str(username or "").strip()
        self._secret = _secret(password)
        if not self._user or not self._secret:
            raise SourceError("source.family.config", "user name and password are needed")
        if not USER_RE.match(self._user):
            raise SourceError("source.family.config", "user name invalid")
        self._t = Transport(self._base, ALLOW, auth_headers=self._auth, allow_self_signed=bool(allow_self_signed),
                            timeout=timeout)
        self.interval = max(5.0, float(interval))
        self.full_interval = max(self.interval, float(full_interval))
        self._skip = {}                  # key -> monotonic time until which the path is not asked
        # the pause belongs to this exact login: a corrected password is tried at once (and a wrong one again never floods)
        self._pkey = hashlib.sha256(("%s\0%s\0%s" % (self._host, self._user, self._secret.reveal())).encode("utf-8")).hexdigest()
        self._cfg = {}                   # the last full poll's replies, merged into light polls
        self._caps = capabilities.validate(CAPABILITIES, self.family)
        self.last_status = {}            # key -> HTTP status of the last poll (for the connection test and tests)
        self.last_areas = ()

    def __repr__(self):
        return "MikrotikReader(%s)" % self._host

    __str__ = __repr__

    def _auth(self) -> dict:
        token = base64.b64encode(("%s:%s" % (self._user, self._secret.reveal())).encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic " + token}

    def _paused(self) -> bool:
        with _LOCK:
            return time.monotonic() < _PAUSED.get(self._pkey, 0.0)

    # ------------------------------------------------------------------ one request
    def _get(self, key: str, path: str, pause: bool = True):
        """Parsed reply, or None when the router does not have the path, the user may not read it or it is no JSON."""
        try:
            status, _, data = self._t.request("GET", path)
        except Forbidden:
            raise SourceError("source.family.refused", "request outside the allow-list") from None
        except TransportError as exc:
            key_ = "source.family.unreachable" if exc.key.endswith("unreachable") else \
                "source.family.tls" if exc.key.endswith("certificate") else "source.family.protocol"
            raise SourceError(key_) from None
        self.last_status[key] = status
        if status == 401:
            if pause:                    # the account check leaves the pause to the poll or test that follows it
                with _LOCK:
                    _PAUSED[self._pkey] = time.monotonic() + PAUSE_S
            raise SourceError("source.family.auth_failed", "user name or password was rejected")
        if status in (400, 403, 404, 405, 500, 501):
            self._skip[key] = time.monotonic() + SKIP_S
            return None
        if not 200 <= status < 300:
            raise SourceError("source.family.protocol", "unexpected status %d" % status)
        try:
            body = json.loads(data.decode("utf-8")) if data else None
        except (ValueError, UnicodeDecodeError):
            raise SourceError("source.family.protocol", "reply is not JSON") from None
        if isinstance(body, dict) and "error" in body and len(body) <= 4:
            self._skip[key] = time.monotonic() + SKIP_S
            return None
        if key in SINGLE:
            body = body[0] if isinstance(body, list) and body and isinstance(body[0], dict) else body
            return scrub(body) if isinstance(body, dict) else None
        return scrub(body) if isinstance(body, list) else None

    # ------------------------------------------------------------------ polls
    def collect(self, light: bool = False) -> dict:
        """Blocking: every reply this poll could get, as the raw dict ``model.to_neutral`` takes. Call from a thread."""
        if self._paused():
            raise SourceError("source.family.paused", "paused after a refused sign-in")
        self.last_status = {}
        t = time.monotonic()
        raw = dict(self._cfg) if light else {}
        asked = dict(LIGHT) if light else dict(LIGHT, **FULL)
        for key, path in asked.items():
            if self._skip.get(key, 0) > t:
                continue
            got = self._get(key, path)
            if got is not None:
                raw[key] = got
            elif key in raw:
                del raw[key]
        if not light:
            self._cfg = {k: v for k, v in raw.items() if k in FULL}
        if asked and self.last_status and all(s == 403 for s in self.last_status.values()):
            raise SourceError("source.family.forbidden", "the user may not read any of the pages Lanloom needs")
        if not raw.get("interfaces") and not raw.get("resource"):
            if self.last_status.get("resource") == 403 or self.last_status.get("interfaces") == 403:
                raise SourceError("source.family.forbidden", "the user may not read the interface list")
            raise SourceError("source.family.protocol", "the router answered neither resources nor interfaces")
        self.last_areas = tuple(sorted(raw))
        return raw

    def capabilities(self) -> dict:
        return self._caps

    def _refine(self, raw: dict, state: dict) -> dict:
        missing = set()
        if not isinstance(raw.get("filter"), list):
            missing.add("policies")
        if not isinstance(raw.get("nat"), list):
            missing.add("port_forwards")
        if not isinstance(raw.get("leases"), list):
            missing.add("dhcp_reservations")
        if not any(isinstance(raw.get(k), list) for k in ("poe", "ethernet", "bridge_ports")):
            missing.add("ports")
        if not any(isinstance(raw.get(k), list) for k in ("routes", "dhcp_clients", "pppoe_clients")):
            missing.add("wans")
        self._caps = capabilities.refine(capabilities.validate(CAPABILITIES, self.family),
                                         version=state["controller"]["version"] or "", read_missing=missing)
        return self._caps

    def config_event(self, raw: dict) -> dict:
        state = model.to_neutral(raw)
        return {"type": "config", "source": SOURCE, "family": self.family, "t": now(), "state": state,
                "capabilities": self._refine(raw, state)}

    async def poll(self, light: bool = False) -> list:
        raw = await asyncio.to_thread(self.collect, light)
        events = [model.topology_event(raw, now())]
        if not light:
            events.append(self.config_event(raw))
        return events

    # ------------------------------------------------------------------ source contract
    async def run(self, emit):
        async def one_session(emit_, status: StatusTracker):
            last_full = None                  # the first poll is always a full one (monotonic time may start near 0)
            while True:
                full = last_full is None or time.monotonic() - last_full >= self.full_interval
                for ev in await self.poll(light=not full):
                    await emit_event(emit_, ev)
                if full:
                    last_full = time.monotonic()
                await status.set(emit_, True, "source.family.ok")
                await asyncio.sleep(self.interval)

        await run_with_backoff(SOURCE, MODULE, emit, one_session)

    async def test(self):
        async def probe():
            raw = await asyncio.to_thread(self.collect, False)
            st = model.to_neutral(raw)
            return {"version": st["controller"]["version"] or None, "site": st["controller"]["site"],
                    "devices": len(st["devices"]), "networks": len(st["networks"]), "ports": len(st["ports"]),
                    "port_forwards": len(st["port_forwards"])}

        return await short_test(MODULE, probe(), self._t.timeout * 30 + 10)

    # ------------------------------------------------------------------ the account
    def scope(self) -> str:
        """Blocking: ``read_only`` | ``can_write`` | ``unknown``, from the policy list of the user's group."""
        if self._paused():
            return "unknown"
        try:
            users = self._get("users", ACCOUNT_PATHS["users"], pause=False)
            groups = self._get("groups", ACCOUNT_PATHS["groups"], pause=False)
        except SourceError:
            return "unknown"
        if not isinstance(users, list) or not isinstance(groups, list):
            return "unknown"
        mine = next((u for u in users if isinstance(u, dict) and u.get("name") == self._user), None)
        grp = next((g for g in groups if isinstance(g, dict) and mine and g.get("name") == mine.get("group")), None)
        if not grp or not isinstance(grp.get("policy"), str):
            return "unknown"
        on = {p.strip() for p in grp["policy"].split(",") if p.strip() and not p.strip().startswith("!")}
        if on & WRITE_POLICIES:
            return "can_write"
        return "read_only" if "read" in on else "unknown"

    async def account_scope(self) -> str:
        """A user whose group can change anything is refused by the app (read-only users only)."""
        return await asyncio.to_thread(self.scope)
