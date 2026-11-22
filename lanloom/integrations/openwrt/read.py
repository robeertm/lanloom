"""Read half of the OpenWrt family: reads the router through its ubus JSON-RPC endpoint, never writes.

Every request goes through ``transport.Transport`` with a *read* allow-list that holds exactly one rule: a JSON-RPC
``call`` document POSTed to ``/ubus`` whose object, method and arguments are on ``READ_CALLS`` - the sign-in, board
and system information, ``uci get`` of four configurations, the live interface and device state, the web interface
helper's host hints and leases, and the wireless state. ``uci set``, ``add``, ``delete``, ``apply``, ``file``,
``rc``, ``system reboot`` and every other object or method are refused before a byte is sent.

The sign-in opens a session of 15 minutes that the polls reuse; at most four sign-ins an hour (a refused password
pauses the half for 15 minutes, so a wrong password never floods the router's log). One full poll every 5 minutes. A
call the login may not make (its access list leaves it out) or a package that is not installed (no wireless, no web
interface helper) is left out from then on for an hour; the other areas still show.

Events: ``{"type": "config", "source": "family", "family": "openwrt", "state": <neutral model v2>, "capabilities": ...}``
- what the firewall tab, the wireless list and the component card show (read only) - and a topology observation
(``{"type": "topology", "source": "controller", ...}``) with the router and its associated wireless clients, so the
map can hang them below it.

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
import asyncio
import json
import re
import time

from . import READ_CAPABILITIES, model
from .. import capabilities
from ..credentials import Secret
from ..transport import Forbidden, Transport, TransportError, read_allowlist
from ... import wifisignal
from ...sources._common import SourceError, StatusTracker, emit_event, now, run_with_backoff, short_test

MODULE = "family"
SOURCE = "family"
URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}(?::\d{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](?::\d{1,5})?)/?\Z", re.I)
USER_RE = re.compile(r"\A[A-Za-z0-9_.@-]{1,64}\Z")
PATH = "/ubus"
FULL_S = 300.0
PAUSE_S = 900.0                  # after a refused password
SKIP_S = 3600.0                  # a call that is not allowed or not installed is asked again after this
SESSION_S = 900                  # session length asked at sign-in (the polls reuse it)
LOGINS_PER_HOUR = 4
MAX_RADIOS = 16
CONFIGS = ("network", "firewall", "dhcp", "wireless")

_IFNAME = lambda v: isinstance(v, str) and bool(model.IFNAME_RE.match(v))      # noqa: E731
READ_CALLS = {
    ("session", "login"): model.LOGIN_ARGS,
    ("system", "board"): {},
    ("system", "info"): {},
    ("uci", "get"): {"config": lambda v: v in CONFIGS},
    ("network.interface", "dump"): {},
    ("network.device", "status"): {},
    ("luci-rpc", "getHostHints"): {},
    ("luci-rpc", "getDHCPLeases"): {},
    ("iwinfo", "devices"): {},
    ("iwinfo", "info"): {"device": _IFNAME},
    ("iwinfo", "assoclist"): {"device": _IFNAME},
}
ALLOW = read_allowlist((), json_query=(re.escape(PATH), dict(model.ENVELOPE, params=model.call_check(READ_CALLS))))
# what a read-only login may be granted besides READ_CALLS (harmless session housekeeping)
HARMLESS = {("session", "access"), ("session", "login"), ("session", "get"), ("session", "list"), ("luci", "getVersion"),
            ("luci", "getFeatures"), ("luci-rpc", "getBoardJSON"), ("luci-rpc", "getNetworkDevices"),
            ("luci-rpc", "getWirelessDevices")}


def _secret(value) -> Secret:
    return value if isinstance(value, Secret) else Secret(value)


def scope_of(acls) -> str:
    """read_only | can_write | unknown, from the access list the router returns at sign-in."""
    if not isinstance(acls, dict) or not acls:
        return "unknown"
    reads = {k for k in READ_CALLS} | HARMLESS
    ubus = acls.get("ubus") if isinstance(acls.get("ubus"), dict) else {}
    for obj, methods in ubus.items():
        for m in (methods if isinstance(methods, list) else [methods]):
            if obj == "*" or m == "*" or (str(obj), str(m)) not in reads:
                return "can_write"
    uci = acls.get("uci") if isinstance(acls.get("uci"), dict) else {}
    for cfg, rights in uci.items():
        if "write" in (rights if isinstance(rights, list) else [rights]):
            return "can_write"
    for area in ("file", "cgi-io"):
        got = acls.get(area)
        if isinstance(got, dict):
            for rights in got.values():
                if set(rights if isinstance(rights, list) else [rights]) & {"write", "exec", "upload", "*"}:
                    return "can_write"
    return "read_only" if ubus else "unknown"


class OpenwrtReader:
    family = "openwrt"
    name = SOURCE

    def __init__(self, url=None, username=None, password=None, allow_self_signed=False, interval=FULL_S, timeout=15.0,
                 *, clock=time.monotonic):
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise SourceError("source.family.config", "address missing or invalid")
        self._base = "%s://%s" % (m.group(1).lower(), m.group(2))
        self._host = m.group(2)
        self._user = str(username.reveal() if isinstance(username, Secret) else username or "").strip()
        self._password = _secret(password)
        if not USER_RE.match(self._user) or not self._password:
            raise SourceError("source.family.config", "login name and password are needed")
        self._t = Transport(self._base, ALLOW, allow_self_signed=bool(allow_self_signed), timeout=timeout)
        self.interval = max(30.0, float(interval))
        self._clock = clock
        self._sid = None                 # the session id (a bearer secret: never logged, never in repr)
        self._acls = None
        self._last_ok = float("-inf")    # monotonic time of the last call the session was accepted for
        self._logins = []                # monotonic times of the sign-ins of the last hour
        self._skip = {}                  # (object, method, config) -> monotonic time until which the call is not made
        self._paused_until = 0.0
        self._n = 0
        self._caps = capabilities.validate(READ_CAPABILITIES, self.family)
        self.last_status = {}            # (object, method[, arg]) -> code of the last poll (for the test and tests)

    def __repr__(self):
        return "OpenwrtReader(%s)" % self._host

    __str__ = __repr__

    # ------------------------------------------------------------------ one call
    def _post(self, sid, obj, method, args):
        self._n = self._n % 999_999_999 + 1
        try:
            status, _, data = self._t.request("POST", PATH, model.call_body(sid, obj, method, args, self._n))
        except Forbidden:
            raise SourceError("source.family.refused", "call outside the allow-list") from None
        except TransportError as exc:
            if exc.key.endswith("unreachable"):
                raise SourceError("source.family.unreachable") from None
            raise SourceError("source.family.protocol") from None
        if status in (401, 403):
            return 6, None
        if status == 404:
            raise SourceError("source.family.protocol", "no ubus endpoint at this address")
        if not 200 <= status < 300:
            raise SourceError("source.family.protocol", "unexpected status %d" % status)
        try:
            body = json.loads(data.decode("utf-8")) if data else None
        except (ValueError, UnicodeDecodeError):
            raise SourceError("source.family.protocol", "reply is not JSON") from None
        return model.reply(body)

    def _login(self) -> None:
        t = self._clock()
        if t < self._paused_until:
            raise SourceError("source.family.paused", "paused after a refused password")
        self._logins = [x for x in self._logins if t - x < 3600]
        if len(self._logins) >= LOGINS_PER_HOUR:
            raise SourceError("source.family.paused", "sign-in limit of the hour reached")
        self._logins.append(t)
        code, data = self._post(model.ZERO_SID, "session", "login",
                                {"username": self._user, "password": self._password.reveal(), "timeout": SESSION_S})
        sid = data.get("ubus_rpc_session") if isinstance(data, dict) else None
        if code != 0 or not isinstance(sid, str) or not model.SID_RE.match(sid):
            self._sid = None
            if code in (6, -32002, 0):
                self._paused_until = t + PAUSE_S
                raise SourceError("source.family.key_refused", "login was rejected")
            raise SourceError("source.family.protocol", "sign-in failed")
        self._sid = sid
        self._last_ok = t
        self._acls = data.get("acls") if isinstance(data.get("acls"), dict) else None

    def call(self, obj, method, args=None):
        """One allowed call with the session (signs in when needed, once more when the session has expired).
        Returns (code, data); code 0 is success."""
        args = dict(args or {})
        if self._sid is None:
            self._login()
        code, data = self._post(self._sid, obj, method, args)
        # "access denied" means either "the session expired" or "the access list leaves this call out"; every call
        # renews the session, so within its lifetime it is the access list - no new sign-in for that
        if code == -32002 and self._clock() - self._last_ok > SESSION_S - 60:
            self._sid = None
            self._login()
            code, data = self._post(self._sid, obj, method, args)
        if code != -32002:
            self._last_ok = self._clock()
        return code, data

    def _get(self, obj, method, args=None, tag=None):
        key = (obj, method, tag)
        if self._skip.get(key, 0) > self._clock():
            return None
        code, data = self.call(obj, method, args)
        self.last_status[key] = code
        if code != 0:
            self._skip[key] = self._clock() + SKIP_S         # not allowed, not installed, not there: ask again later
            return None
        return model.scrub(data) if isinstance(data, (dict, list)) else None

    # ------------------------------------------------------------------ polls
    def collect(self) -> dict:
        """Blocking: every reply this poll could get, as the raw dict ``model.to_neutral`` takes. Call from a thread."""
        if self._clock() < self._paused_until:
            raise SourceError("source.family.paused", "paused after a refused password")
        self.last_status = {}
        raw = {"board": self._get("system", "board"), "info": self._get("system", "info")}
        for cfg in CONFIGS:
            got = self._get("uci", "get", {"config": cfg}, cfg)
            raw[cfg] = got.get("values") if isinstance(got, dict) and isinstance(got.get("values"), dict) else None
        raw["ifaces"] = self._get("network.interface", "dump")
        raw["devices"] = self._get("network.device", "status")
        raw["hints"] = self._get("luci-rpc", "getHostHints")
        raw["leases"] = self._get("luci-rpc", "getDHCPLeases")
        devs = self._get("iwinfo", "devices")
        if isinstance(devs, dict) and isinstance(devs.get("devices"), list):
            air = {}
            for dev in [d for d in devs["devices"] if isinstance(d, str) and model.IFNAME_RE.match(d)][:MAX_RADIOS]:
                info = self._get("iwinfo", "info", {"device": dev}, dev)
                if not isinstance(info, dict) or str(info.get("mode") or "").lower() not in ("master", "ap"):
                    continue
                assoc = self._get("iwinfo", "assoclist", {"device": dev}, "a:" + dev)
                rows = assoc.get("results") if isinstance(assoc, dict) else None
                air[dev] = {"info": info, "assoc": rows if isinstance(rows, list) else []}
            raw["air"] = air
        board = raw["board"] if isinstance(raw["board"], dict) else {}
        rel = board.get("release") if isinstance(board.get("release"), dict) else {}
        raw["version"] = str(rel.get("version") or "")[:48]
        if raw["firewall"] is None and raw["network"] is None and not board:
            raise SourceError("source.family.forbidden", "the login may not read any of the areas Lanloom needs")
        return raw

    def capabilities(self) -> dict:
        return self._caps

    def _refine(self, raw: dict, state: dict) -> dict:
        missing = set()
        if raw.get("firewall") is None:
            missing |= {"zones", "policies", "port_forwards", "firewall"}
        if raw.get("wireless") is None:
            missing.add("ssids")
        if raw.get("dhcp") is None:
            missing.add("dhcp_reservations")
        if raw.get("leases") is None and raw.get("hints") is None and raw.get("air") is None:
            missing.add("clients")
        self._caps = capabilities.refine(capabilities.validate(READ_CAPABILITIES, self.family),
                                         version=state["controller"]["version"] or "", read_missing=missing)
        return self._caps

    def config_event(self, raw: dict) -> dict:
        state = model.to_neutral(raw)
        return {"type": "config", "source": SOURCE, "family": self.family, "t": now(), "state": state,
                "capabilities": self._refine(raw, state)}

    @staticmethod
    def topology_event(raw: dict, t: float):
        """The router and its associated wireless clients, in the shape of the generic controller source (the
        resolver ranks it as ``controller``). None when the router's own hardware address is not readable."""
        topo = model.Topology(raw)
        mac = model.router_mac(raw, topo)
        if not mac:
            return None
        eq = model.to_neutral(raw)["equipment"]
        e = eq[0] if eq else {}
        known = {}
        for r in ((raw.get("leases") or {}).get("dhcp_leases") or [] if isinstance(raw.get("leases"), dict) else []):
            m = model.norm_mac(r.get("macaddr")) if isinstance(r, dict) else None
            if m:
                known[m] = (model.ip_of(r.get("ipaddr")), model.text(r.get("hostname"), 64))
        clients = []
        for m, w in model._wireless_clients(raw).items():
            ip, name = known.get(m, (None, None))
            clients.append({"mac": m, "ip": ip, "name": name, "medium": "radio", "parent_mac": mac, "port": None,
                            "band": w["band"], "ssid": w["ssid"], "signal": wifisignal.clean(w.get("signal"))})
        dev = {"mac": mac, "ip": e.get("mgmt_ip"), "name": e.get("name"), "role": e.get("kind") or "router",
               "model": e.get("model"), "uplink": None}
        return {"type": "topology", "source": "controller", "t": t, "devices": [dev], "clients": clients[:model.MAX_ITEMS]}

    async def poll(self, light: bool = False) -> list:
        raw = await asyncio.to_thread(self.collect)
        out = [self.config_event(raw)]
        topo = self.topology_event(raw, now())
        if topo:
            out.append(topo)
        return out

    # ------------------------------------------------------------------ source contract
    async def run(self, emit):
        async def one_session(emit_, status: StatusTracker):
            while True:
                for ev in await self.poll():
                    await emit_event(emit_, ev)
                await status.set(emit_, True, "source.family.ok")
                await asyncio.sleep(self.interval)

        await run_with_backoff(SOURCE, MODULE, emit, one_session)

    async def test(self):
        async def probe():
            raw = await asyncio.to_thread(self.collect)
            st = model.to_neutral(raw)
            if not st["zones"] and not st["networks"]:
                raise SourceError("source.family.forbidden", "nothing readable")
            return {"version": st["controller"]["version"] or None, "zones": len(st["zones"]), "networks": len(st["networks"]),
                    "policies": len([p for p in st["policies"] if not p.get("builtin")]), "ssids": len(st["ssids"]),
                    "clients": len(st["devices"])}

        return await short_test(MODULE, probe(), self._t.timeout * 3 + 20)

    def scope(self) -> str:
        """Blocking: read_only | can_write | unknown, from the access list of the session."""
        if self._acls is None:
            try:
                self._login()
            except SourceError:
                return "unknown"
        return scope_of(self._acls)

    async def account_scope(self) -> str:
        """A login whose access list allows any change is refused by the app (read-only logins only)."""
        return await asyncio.to_thread(self.scope)
