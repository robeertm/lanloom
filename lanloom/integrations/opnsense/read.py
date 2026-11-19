"""Read half of the OPNsense family: reads the firewall through its REST API, never writes.

Every request goes through ``transport.Transport`` with a *read* allow-list: GET on the fixed paths below and nothing
else - no POST at all (the API needs no sign-in: the key and secret travel as Basic authentication on every request),
so no change, no apply, no reboot, no firmware action can leave this process.

One full poll every 5 minutes reads every path once. A path the firewall does not have (an older release, a plug-in
that is not installed) or the key may not read (a privilege that was not granted) is left out from then on for an
hour, the other areas still show. A refused key (HTTP 401) pauses the half for 15 minutes, so a wrong secret never
floods the firewall's log with failed attempts.

Event: ``{"type": "config", "source": "family", "family": "opnsense", "state": <neutral model v2>, "capabilities": ...}``
- what the firewall tab shows (read only).

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
import asyncio
import base64
import json
import re
import time

from . import READ_CAPABILITIES, model
from .. import capabilities
from ..credentials import Secret
from ..transport import Forbidden, Transport, TransportError, read_allowlist
from ...sources._common import SourceError, StatusTracker, emit_event, now, run_with_backoff, short_test

MODULE = "family"
SOURCE = "family"
URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}(?::\d{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](?::\d{1,5})?)/?\Z", re.I)
KEY_RE = re.compile(r"\A[A-Za-z0-9+/=]{16,128}\Z")
FULL_S = 300.0
PAUSE_S = 900.0                  # after a refused key
SKIP_S = 3600.0                  # a path that is missing or not allowed is asked again after this

# key -> path; every one optional (the area simply stays empty)
PATHS = {
    "system": "/api/diagnostics/system/systemInformation",
    "firmware": "/api/core/firmware/status",
    "interfaces": "/api/interfaces/overview/interfacesInfo",
    "filter": "/api/firewall/filter/get",
    "rules": "/api/firewall/filter/searchRule",
    "groups": "/api/firewall/group/searchItem",
    "aliases": "/api/firewall/alias/get",
    "vlans": "/api/interfaces/vlan_settings/searchItem",
    "snat": "/api/firewall/source_nat/searchRule",
    "dnat": "/api/firewall/d_nat/searchRule",
    "kea": "/api/kea/dhcpv4/searchReservation",
    "dnsmasq": "/api/dnsmasq/settings/searchHost",
    "routes": "/api/routes/routes/searchroute",
    "gateways": "/api/routes/gateway/status",
    "vpn_tunnel": "/api/wireguard/server/searchServer",
    "vpn_ipsec": "/api/ipsec/connections/searchConnection",
    "vpn_ssl": "/api/openvpn/instances/search",
}
ALLOW = read_allowlist(tuple(re.escape(p) for p in PATHS.values()))
# reply keys that carry secrets in some releases (never kept, not even in the collected raw dict)
SECRET_KEYS = re.compile(r"(?i)(privkey|private|secret|psk|pre_shared|password|passwd|pubkey|key$|cert|crt|token)")


def _secret(value) -> Secret:
    return value if isinstance(value, Secret) else Secret(value)


def scrub(obj, depth=0):
    """A copy of a reply without any field whose name says it carries a key, a secret or a certificate. Only fields
    with a plain value are dropped: a key that maps to an object is data (a rule's id, an option such as an alias
    named ``private_nets``), not a field name."""
    if depth > 12:
        return None
    if isinstance(obj, dict):
        return {k: scrub(v, depth + 1) for k, v in obj.items() if isinstance(v, dict) or not SECRET_KEYS.search(str(k))}
    if isinstance(obj, list):
        return [scrub(v, depth + 1) for v in obj[:model.MAX_ITEMS]]
    return obj


class OpnsenseReader:
    family = "opnsense"
    name = SOURCE

    def __init__(self, url=None, api_key=None, api_secret=None, allow_self_signed=False, interval=FULL_S, timeout=15.0):
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise SourceError("source.family.config", "address missing or invalid")
        self._base = "%s://%s" % (m.group(1).lower(), m.group(2))
        self._host = m.group(2)
        self._key = str(api_key.reveal() if isinstance(api_key, Secret) else api_key or "").strip()
        self._secret = _secret(api_secret)
        if not KEY_RE.match(self._key) or not self._secret or not KEY_RE.match(self._secret.reveal()):
            raise SourceError("source.family.config", "API key and secret are needed")
        self._t = Transport(self._base, ALLOW, auth_headers=self._auth, allow_self_signed=bool(allow_self_signed),
                            timeout=timeout)
        self.interval = max(30.0, float(interval))
        self._skip = {}                  # key -> monotonic time until which the path is not asked
        self._paused_until = 0.0
        self._caps = capabilities.validate(READ_CAPABILITIES, self.family)
        self.last_status = {}            # key -> HTTP status of the last poll (for the connection test and tests)

    def __repr__(self):
        return "OpnsenseReader(%s)" % self._host

    __str__ = __repr__

    def _auth(self) -> dict:
        token = base64.b64encode(("%s:%s" % (self._key, self._secret.reveal())).encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic " + token}

    # ------------------------------------------------------------------ one request
    def _get(self, key: str):
        try:
            status, _, data = self._t.request("GET", PATHS[key])
        except Forbidden:
            raise SourceError("source.family.refused", "request outside the allow-list") from None
        except TransportError as exc:
            if exc.key.endswith("unreachable"):
                raise SourceError("source.family.unreachable") from None
            raise SourceError("source.family.protocol") from None
        self.last_status[key] = status
        if status == 401:
            self._paused_until = time.monotonic() + PAUSE_S
            raise SourceError("source.family.key_refused", "API key was rejected")
        if status in (403, 404, 405, 500, 501):
            self._skip[key] = time.monotonic() + SKIP_S
            return None
        if not 200 <= status < 300:
            raise SourceError("source.family.protocol", "unexpected status %d" % status)
        try:
            body = json.loads(data.decode("utf-8")) if data else None
        except (ValueError, UnicodeDecodeError):
            self._skip[key] = time.monotonic() + SKIP_S       # a page instead of JSON: the path does not exist here
            return None
        if isinstance(body, dict) and body.get("errorMessage") and len(body) <= 3:
            self._skip[key] = time.monotonic() + SKIP_S
            return None
        return scrub(body)

    # ------------------------------------------------------------------ polls
    def collect(self) -> dict:
        """Blocking: every reply this poll could get, as the raw dict ``model.to_neutral`` takes. Call from a thread."""
        if time.monotonic() < self._paused_until:
            raise SourceError("source.family.paused", "paused after a refused key")
        self.last_status = {}
        got = {}
        t = time.monotonic()
        for key in PATHS:
            if self._skip.get(key, 0) > t:
                continue
            got[key] = self._get(key)
        raw = {"version": self._version(got)}
        flt = got.get("filter")
        raw["filter"] = flt.get("filter") if isinstance(flt, dict) and isinstance(flt.get("filter"), dict) else None
        al = got.get("aliases")
        raw["aliases"] = al.get("alias") if isinstance(al, dict) and isinstance(al.get("alias"), dict) else None
        for key in ("interfaces", "rules", "groups", "vlans", "snat", "dnat", "kea", "dnsmasq", "routes", "gateways",
                    "vpn_tunnel", "vpn_ipsec", "vpn_ssl"):
            raw[key] = got.get(key)
        asked = [k for k in got if k not in ("system", "firmware")]
        if asked and all(self.last_status.get(k) == 403 for k in asked):
            raise SourceError("source.family.forbidden", "the key may not read any of the pages Lanloom needs")
        return raw

    @staticmethod
    def _version(got) -> str:
        sysinfo = got.get("system")
        if isinstance(sysinfo, dict):
            for v in sysinfo.get("versions") or []:
                if model.parse_version(v):
                    return str(v)[:48]
        fw = got.get("firmware")
        if isinstance(fw, dict):
            for k in ("product_version", "os_version"):
                if model.parse_version(fw.get(k)):
                    return str(fw[k])[:48]
            prod = fw.get("product")
            if isinstance(prod, dict) and model.parse_version(prod.get("product_version")):
                return str(prod["product_version"])[:48]
        return ""

    def capabilities(self) -> dict:
        return self._caps

    def _refine(self, raw: dict, state: dict) -> dict:
        missing = set()
        if not state["zones"]:
            missing.add("zones")
        if not (raw.get("filter") or raw.get("rules")):
            missing.add("policies")
        if raw.get("dnat") is None:
            missing.add("port_forwards")
        if raw.get("kea") is None and raw.get("dnsmasq") is None:
            missing.add("dhcp_reservations")
        if not any(raw.get(k) is not None for k in ("vpn_tunnel", "vpn_ipsec", "vpn_ssl")):
            missing.add("vpn")
        self._caps = capabilities.refine(capabilities.validate(READ_CAPABILITIES, self.family),
                                         version=state["controller"]["version"] or "", read_missing=missing)
        return self._caps

    def config_event(self, raw: dict) -> dict:
        state = model.to_neutral(raw)
        return {"type": "config", "source": SOURCE, "family": self.family, "t": now(), "state": state,
                "capabilities": self._refine(raw, state)}

    async def poll(self, light: bool = False) -> list:
        raw = await asyncio.to_thread(self.collect)
        return [self.config_event(raw)]

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
            if not st["zones"] and not st["policies"]:
                raise SourceError("source.family.forbidden", "nothing readable")
            return {"version": st["controller"]["version"] or None, "zones": len(st["zones"]), "networks": len(st["networks"]),
                    "policies": len({p["vendor_ref"] for p in st["policies"]}), "port_forwards": len(st["port_forwards"])}

        return await short_test(MODULE, probe(), self._t.timeout * 3 + 20)

    async def account_scope(self) -> str:
        """The firewall grants privileges per page and does not separate reading from writing, and the API does not
        tell a key its own privileges; so this reports ``unknown``. The guide asks for a user with only the pages
        Lanloom reads, and the transport cannot send anything but GET either way."""
        return "unknown"
