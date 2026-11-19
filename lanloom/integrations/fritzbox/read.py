"""Read half of the FRITZ!Box family: reads the box through TR-064 and its mesh topology, never writes.

Every request goes through ``transport.Transport`` with a *read* allow-list (see ``ALLOW``):

* ``POST`` to the control path of one service with a SOAP call that is a row of a fixed table of ``Get`` actions
  (device info, LAN settings, internet connection state, the two actions that hand out the path of the host list and
  of the mesh list). The transport builds the envelope from the checked row; the adapter never hands over XML, and a
  call that is not in the table (a ``Set``, ``Add``, ``Delete``, ``Reboot``, ``Reconnect`` ...) is refused before a byte is sent;
* ``GET`` of the two documents those actions point to (the host list as XML, the mesh topology as JSON), whose path
  must match one exact pattern.

The box asks for HTTP digest authentication on the SOAP calls. The transport answers the challenge (and reuses it),
the user name and password stay in this object and are handed over through a callable. A refused sign-in pauses all
further attempts for 15 minutes: the box delays logins after wrong passwords, so a wrong password must never be
retried in a loop. Sign-ins answered from a fresh challenge are capped per hour as well.

The box has no read-only user. TR-064 needs an account with the right to change the box settings, so the guide asks for
an account made only for Lanloom; what keeps Lanloom from changing anything is its own side: the allow-list above.

Cadence: hosts and mesh every 60 s (the "light" poll), everything else every 5 minutes. Events:

* ``{"type": "topology", "source": "controller", ...}`` - same shape as the generic controller source, so the topology
  resolver merges it with manual links, LLDP and address tables;
* ``{"type": "config", "source": "family", "family": "fritzbox", "state": <neutral model v2>, ...}`` - what the inventory
  shows (read only).

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
import asyncio
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
URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])(?::(\d{1,5}))?/?\Z", re.I)
USER_RE = re.compile(r"\A[^\x00-\x1f\"\\]{1,64}\Z")
DEFAULT_PORTS = {"http": 49000, "https": 49443}
LIGHT_S = 60.0
FULL_S = 300.0
LOGINS_PER_HOUR = 60
PAUSE_AFTER_REFUSAL_S = 900.0

# TR-064 services: control path and service type (the box's own description lists the same pairs)
HOSTS = ("/upnp/control/hosts", "urn:dslforum-org:service:Hosts:1")
DEVICEINFO = ("/upnp/control/deviceinfo", "urn:dslforum-org:service:DeviceInfo:1")
LAN = ("/upnp/control/lanhostconfigmgm", "urn:dslforum-org:service:LANHostConfigManagement:1")
LANIF = ("/upnp/control/lanethernetifcfg", "urn:dslforum-org:service:LANEthernetInterfaceConfig:1")
WANIP = ("/upnp/control/wanipconnection1", "urn:dslforum-org:service:WANIPConnection:1")
WANPPP = ("/upnp/control/wanpppconn1", "urn:dslforum-org:service:WANPPPConnection:1")
CALLS = {                                   # service -> the Get actions that may be called (no arguments)
    HOSTS: ("X_AVM-DE_GetHostListPath", "X_AVM-DE_GetMeshListPath"),
    DEVICEINFO: ("GetInfo",),
    LAN: ("GetInfo",),
    LANIF: ("GetInfo",),
    WANIP: ("GetStatusInfo",),
    WANPPP: ("GetStatusInfo",),
}
LIST_PATH_RE = r"/(?:[a-z]{1,16}/)?(?:hostlist|meshlist)\.lua\?sid=[0-9a-f]{16}"
ALLOW = read_allowlist(
    (LIST_PATH_RE,),
    soap=[(re.escape(path), {(stype, action): {} for action in actions}) for (path, stype), actions in CALLS.items()])

_LOCK = threading.Lock()
_LEDGER = {}                                # key -> ([sign-in times], pause-until)
DENIED = object()                           # the account may not call this action


def _secret(value) -> Secret:
    return value if isinstance(value, Secret) else Secret(value)


def _key(host: str, user: str, secret: str) -> str:
    return hashlib.sha256(("%s\0%s\0%s" % (host, user, secret)).encode("utf-8")).hexdigest()


def _reset_for_tests() -> None:
    with _LOCK:
        _LEDGER.clear()


def fritzos_version(software) -> str:
    """``154.07.57`` (the box's software version) as the version the box shows: ``7.57``."""
    m = re.fullmatch(r"\d+\.(\d+)\.(\d+)", str(software or "").strip())
    return "%d.%s" % (int(m.group(1)), m.group(2)) if m else model.text(software, 32)


class FritzboxReader:
    family = "fritzbox"
    name = SOURCE

    def __init__(self, url=None, username=None, password=None, allow_self_signed=False, interval=LIGHT_S,
                 full_interval=FULL_S, timeout=15.0):
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise SourceError("source.family.config", "address missing or invalid")
        scheme = m.group(1).lower()
        port = int(m.group(3)) if m.group(3) else DEFAULT_PORTS[scheme]
        if not 0 < port < 65536:
            raise SourceError("source.family.config", "port invalid")
        self._host = m.group(2)
        self._base = "%s://%s:%d" % (scheme, self._host, port)
        self._user = str(username or "").strip()
        self._secret = _secret(password)
        if not self._user or not self._secret:
            raise SourceError("source.family.config", "user name and password are needed")
        if not USER_RE.match(self._user):
            raise SourceError("source.family.config", "user name invalid")
        self._t = Transport(self._base, ALLOW, allow_self_signed=bool(allow_self_signed), timeout=timeout,
                            digest=lambda: (self._user, self._secret.reveal()))
        self._lkey = _key(self._host, self._user, self._secret.reveal())
        self.interval = max(5.0, float(interval))
        self.full_interval = max(self.interval, float(full_interval))
        self._caps = capabilities.validate(CAPABILITIES, self.family)
        self._static = {}                   # info, lan, lan_mac, wans of the last full poll
        self.last_areas = ()

    def __repr__(self):
        return "FritzboxReader(%s)" % self._host

    __str__ = __repr__

    # ------------------------------------------------------------------ sign-in bookkeeping
    def _before(self) -> None:
        t = time.time()
        with _LOCK:
            times, pause = _LEDGER.get(self._lkey, ([], 0.0))
            times = [x for x in times if t - x < 3600]
            _LEDGER[self._lkey] = (times, pause)
            if t < pause or len(times) >= LOGINS_PER_HOUR:
                raise SourceError("source.family.paused", "sign-in paused")

    def _after(self, challenges_before: int, refused: bool) -> None:
        t = time.time()
        with _LOCK:
            times, pause = _LEDGER.get(self._lkey, ([], 0.0))
            times = times + [t] * max(0, self._t.challenges - challenges_before)
            if refused:
                pause = t + PAUSE_AFTER_REFUSAL_S
            _LEDGER[self._lkey] = (times, pause)

    # ------------------------------------------------------------------ requests
    def _send(self, method: str, path: str, body=None, headers=None):
        try:
            return self._t.request(method, path, body, headers=headers)
        except Forbidden:
            raise SourceError("source.family.refused", "request outside the allow-list") from None
        except TransportError as exc:
            key = "source.family.unreachable" if exc.key.endswith("unreachable") else \
                "source.family.tls" if exc.key.endswith("certificate") else "source.family.protocol"
            raise SourceError(key) from None

    def _soap(self, service: tuple, action: str):
        """Out arguments of one call (dict); None when the box does not offer it; DENIED when the account may not call it."""
        self._before()
        before = self._t.challenges
        status, _, data = self._send("POST", service[0], {"service": service[1], "action": action, "args": {}})
        refused = status == 401
        self._after(before, refused)
        if refused:
            raise SourceError("source.family.auth_failed", "sign-in refused")
        if status == 403:
            return DENIED
        if status in (404, 405):
            return None
        parsed = model.parse_soap(data)
        if parsed is None:
            if 200 <= status < 300 or status == 500:
                raise SourceError("source.family.protocol", "reply is not a SOAP answer")
            raise SourceError("source.family.protocol", "unexpected status %d" % status)
        fault = parsed.get("_fault")
        if fault is not None:
            if fault == model.FAULT_NOT_AUTHORIZED:
                return DENIED
            return None                                 # unknown action, action failed: this release does not offer it
        if not 200 <= status < 300:
            raise SourceError("source.family.protocol", "unexpected status %d" % status)
        return parsed

    def _document(self, path, accept: str):
        """Body of a document whose path a call handed out (checked against the allow-list pattern); None on any problem."""
        if not isinstance(path, str) or not re.fullmatch(LIST_PATH_RE, path):
            return None
        status, _, data = self._send("GET", path, headers={"Accept": accept})
        return data if status == 200 else None

    # ------------------------------------------------------------------ polls
    def _hosts(self):
        out = self._soap(HOSTS, "X_AVM-DE_GetHostListPath")
        if out is DENIED:
            raise SourceError("source.family.forbidden", "the account may not read the host list")
        if out is None:
            raise SourceError("source.family.protocol", "the box offers no host list")
        doc = self._document(out.get("NewX_AVM-DE_HostListPath"), "text/xml, application/xml")
        rows = model.parse_hostlist(doc) if doc else None
        if rows is None:
            raise SourceError("source.family.protocol", "the host list could not be read")
        return rows

    def _mesh(self):
        out = self._soap(HOSTS, "X_AVM-DE_GetMeshListPath")
        if not isinstance(out, dict):
            return None                                 # an older release has no mesh list; hosts alone still show
        doc = self._document(out.get("NewX_AVM-DE_MeshListPath"), "application/json")
        try:
            mesh = json.loads(doc.decode("utf-8")) if doc else None
        except (ValueError, UnicodeDecodeError):
            return None
        return mesh if isinstance(mesh, dict) and isinstance(mesh.get("nodes"), list) else None

    def _read_static(self) -> dict:
        info = self._soap(DEVICEINFO, "GetInfo")
        if info is DENIED:
            raise SourceError("source.family.forbidden", "the account may not read the device information")
        if info is None:
            raise SourceError("source.family.protocol", "the box does not answer TR-064 (switch it on in the home network settings)")
        raw = {"info": {"model": model.text(info.get("NewModelName"), 48), "firmware": fritzos_version(info.get("NewSoftwareVersion")),
                        "uptime_s": model.num(info.get("NewUpTime"))}}
        lan = self._soap(LAN, "GetInfo")
        if isinstance(lan, dict):
            routers = [x.strip() for x in str(lan.get("NewIPRouters") or "").split(",") if x.strip()]
            raw["lan"] = {"ip": routers[0] if routers else None, "mask": lan.get("NewSubnetMask"),
                          "dhcp": model.flag(lan.get("NewDHCPServerEnable")), "first": lan.get("NewMinAddress"),
                          "last": lan.get("NewMaxAddress")}
        eth = self._soap(LANIF, "GetInfo")
        if isinstance(eth, dict):
            raw["lan_mac"] = eth.get("NewMACAddress")
        wans = []
        for kind, service in (("ip", WANIP), ("ppp", WANPPP)):
            res = self._soap(service, "GetStatusInfo")
            if isinstance(res, dict):
                wans.append({"kind": kind, "status": res.get("NewConnectionStatus"), "uptime_s": model.num(res.get("NewUptime")),
                             "error": model.text(res.get("NewLastConnectionError"), 48)})
        raw["wans"] = wans
        return raw

    def collect(self, light: bool = False) -> dict:
        """Blocking: every reply this poll could get (keys see ``model``). Call from a thread."""
        raw = {"hosts": self._hosts()}
        mesh = self._mesh()
        if mesh is not None:
            raw["mesh"] = mesh
        if not light or not self._static:
            self._static = self._read_static()
        raw.update(self._static)
        self.last_areas = tuple(sorted(k for k, v in raw.items() if v))
        return raw

    def capabilities(self) -> dict:
        return self._caps

    def _refine(self, raw: dict, state: dict) -> dict:
        missing = set()
        if not state["networks"]:
            missing.add("networks")
        if not state["wans"]:
            missing.add("wans")
        caps = capabilities.refine(capabilities.validate(CAPABILITIES, self.family), version=state["controller"]["version"] or "",
                                   read_missing=missing)
        self._caps = caps
        return caps

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
            return {"version": st["controller"]["version"] or None, "model": (raw.get("info") or {}).get("model"),
                    "devices": len(st["devices"]), "equipment": len(st["equipment"]), "mesh": st["controller"]["mesh"]}

        return await short_test(MODULE, probe(), self._t.timeout * 12 + 10)

    async def account_scope(self) -> str:
        """TR-064 has no read-only user and does not tell the rights of the account, so this reports ``unknown``; the
        guide asks for an account made only for Lanloom. Lanloom's own side cannot send a change in any case."""
        return "unknown"
