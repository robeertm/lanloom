"""Read half of the UniFi family: reads the controller through its two local APIs, never writes.

Every request goes through ``transport.Transport`` with a *read* allow-list (GET on the paths below, plus the
sign-in and sign-out of the classic API whose body is exactly user name and password). Nothing else can be sent.

Cadence: the light poll (topology and statistics: clients and devices) every 60 s, the full poll (networks, zones,
policies, wireless networks, port profiles, forwarding, fixed addresses) every 5 minutes. Events:

* ``{"type": "topology", "source": "controller", ...}`` - same shape as the generic controller source, so the
  topology resolver merges it with manual links, LLDP and address tables;
* ``{"type": "config", "source": "family", "family": "unifi", "state": <neutral model v2>, "capabilities": ...}``
  - what the firewall tab shows (read only).

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
import asyncio
import json
import re
import time

from . import CAPABILITIES, READ_ONLY_WRITE_KEYS, model, session
from .. import capabilities
from ..credentials import Secret
from ..transport import Forbidden, Transport, TransportError, read_allowlist
from ...sources._common import SourceError, StatusTracker, emit_event, now, run_with_backoff, short_test

MODULE = "family"
SOURCE = "family"
SITE_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}(?::\d{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](?::\d{1,5})?)/?\Z", re.I)
LIGHT_S = 60.0
FULL_S = 300.0
PAGE = 200
MAX_PAGES = 25
MAX_DETAILS = 200

_S = r"[A-Za-z0-9_-]{1,64}"                              # site name (classic) or site id (official)
_ID = r"[A-Za-z0-9-]{1,64}"
_Q = r"(?:\?offset=\d{1,6}&limit=\d{1,4})?"
_P = r"(?:/proxy/network)?"
CLASSIC_GET = (
    _P + r"/api/s/%s/(?:self|stat/(?:sta|device|sysinfo)|get/setting/ips|rest/(?:networkconf|wlanconf|portconf|portforward|user|firewallrule|firewallgroup))" % _S,
    _P + r"/v2/api/site/%s/(?:firewall/zone|firewall-policies)" % _S,
)
OFFICIAL_GET = (
    _P + r"/integration/v1/(?:info|sites%s)" % _Q,
    _P + r"/integration/v1/sites/%s/(?:networks|firewall/zones|firewall/policies|wifi/broadcasts|devices|clients|wans|"
         r"vpn/servers|vpn/site-to-site-tunnels|acl-rules|dns/policies)%s" % (_ID, _Q),
    _P + r"/integration/v1/sites/%s/(?:networks|devices|wifi/broadcasts)/%s" % (_ID, _ID),
)
ALLOW = read_allowlist(CLASSIC_GET + OFFICIAL_GET, login=(r"/api/(?:auth/)?login", ("username", "password")),
                       logout=r"/api/(?:auth/)?logout")
LOGIN = {"console": "/api/auth/login", "classic": "/api/login"}
LOGOUT = {"console": "/api/auth/logout", "classic": "/api/logout"}
PREFIX = {"console": "/proxy/network", "classic": ""}
CLASSIC_AREAS = {"sysinfo": "api/s/{site}/stat/sysinfo", "device": "api/s/{site}/stat/device", "sta": "api/s/{site}/stat/sta",
                 "networkconf": "api/s/{site}/rest/networkconf", "wlanconf": "api/s/{site}/rest/wlanconf",
                 "portconf": "api/s/{site}/rest/portconf", "portforward": "api/s/{site}/rest/portforward",
                 "user": "api/s/{site}/rest/user", "firewallgroup": "api/s/{site}/rest/firewallgroup",
                 "firewallrule": "api/s/{site}/rest/firewallrule", "zone": "v2/api/site/{site}/firewall/zone",
                 "policies": "v2/api/site/{site}/firewall-policies", "ips": "api/s/{site}/get/setting/ips"}
LIGHT_CLASSIC = ("device", "sta")
OFFICIAL_LISTS = {"o_zones": "firewall/zones", "o_policies": "firewall/policies", "o_wans": "wans", "o_acl": "acl-rules",
                  "o_dns": "dns/policies", "o_clients": "clients"}
OFFICIAL_DETAILS = {"o_networks": "networks", "o_devices": "devices", "o_wifi": "wifi/broadcasts"}


def _secret(value) -> Secret:
    return value if isinstance(value, Secret) else Secret(value)


class UnifiReader:
    family = "unifi"
    name = SOURCE

    def __init__(self, url=None, site="default", username=None, password=None, api_key=None,
                 allow_self_signed=False, interval=LIGHT_S, full_interval=FULL_S, timeout=10.0):
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise SourceError("source.family.config", "address missing or invalid")
        self._base = "%s://%s" % (m.group(1).lower(), m.group(2))
        self._host = m.group(2)
        site = str(site or "default").strip()
        if not SITE_RE.match(site):
            raise SourceError("source.family.config", "site invalid")
        self._site = site
        self._user = str(username or "").strip()
        self._password = _secret(password)
        self._key = _secret(api_key)
        if len(self._user) > 128 or any(ord(c) < 32 for c in self._user):
            raise SourceError("source.family.config", "user name invalid")
        if bool(self._user) != bool(self._password):
            raise SourceError("source.family.config", "user name and password belong together")
        if not self._key and not self._user:
            raise SourceError("source.family.config", "an API key or a view-only account is needed")
        self._t = Transport(self._base, ALLOW, allow_self_signed=bool(allow_self_signed), timeout=timeout)
        self._session = session.shared(self._host, self._user, self._password.reveal()) if self._user else None
        self.interval = max(5.0, float(interval))
        self.full_interval = max(self.interval, float(full_interval))
        self._official_prefix = None      # "/proxy/network" | "" once found; False = not available
        self._site_id = None
        self._caps = capabilities.refine(capabilities.validate(CAPABILITIES, self.family), write_missing=READ_ONLY_WRITE_KEYS)
        self.last_raw_keys = ()

    def __repr__(self):
        return "UnifiReader(%s, site=%r)" % (self._host, self._site)

    __str__ = __repr__

    # ------------------------------------------------------------------ one request
    def _get(self, path: str, official: bool = False):
        heads = {"X-API-KEY": self._key.reveal()} if official else (self._session.headers() if self._session else {})
        try:
            status, rheads, data = self._t.request("GET", path, headers=heads)
        except Forbidden:
            raise SourceError("source.family.refused", "request outside the allow-list") from None
        except TransportError as exc:
            raise SourceError("source.family.unreachable" if exc.key.endswith("unreachable") else "source.family.protocol") from None
        if not official and self._session:
            self._session.absorb(rheads)
        body = None
        if data:
            try:
                body = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                if 200 <= status < 300:
                    raise SourceError("source.family.protocol", "reply is not JSON") from None
        return status, body

    # ------------------------------------------------------------------ classic API
    def _sign_in(self) -> None:
        s = self._session
        try:
            session.may_sign_in(s)
        except session.LoginPaused:
            raise SourceError("source.family.paused", "sign-in paused") from None
        s.forget()
        order = [s.style] if s.style else ["console", "classic"]
        creds = {"username": self._user, "password": self._password.reveal()}
        refused = False
        for i, style in enumerate(order):
            try:
                status, rheads, data = self._t.request("POST", LOGIN[style], creds)
            except TransportError as exc:
                raise SourceError("source.family.unreachable" if exc.key.endswith("unreachable") else "source.family.protocol") from None
            more = i + 1 < len(order)
            if more and status in (401, 404, 405):
                refused = refused or status == 401
                continue
            if status in (400, 401, 403) or (refused and status in (404, 405)):
                session.refused(s)
                raise SourceError("source.family.auth_failed", "sign-in refused")
            if not 200 <= status < 300:
                raise SourceError("source.family.protocol", "unexpected sign-in status %d" % status)
            s.absorb(rheads)
            if not s.headers().get("Cookie"):
                raise SourceError("source.family.protocol", "sign-in gave no session")
            s.started(style)
            return
        raise SourceError("source.family.protocol", "no known sign-in endpoint")

    def _classic(self, area: str, relogin: list):
        s = self._session
        if not s.signed_in:
            self._sign_in()
        path = "%s/%s" % (PREFIX[s.style], CLASSIC_AREAS[area].format(site=self._site))
        status, body = self._get(path)
        if status == 401 and relogin[0]:
            relogin[0] = False
            self._sign_in()
            status, body = self._get(path)
        if status == 401:
            s.forget()
            raise SourceError("source.family.auth_failed", "session refused")
        if status == 403:
            raise SourceError("source.family.forbidden", "account may not read this")
        if status in (400, 404, 405):
            return None                                   # not offered by this release (older or newer)
        if not 200 <= status < 300:
            raise SourceError("source.family.protocol", "unexpected status %d" % status)
        if isinstance(body, dict) and isinstance(body.get("meta"), dict) and body["meta"].get("rc") not in (None, "ok"):
            if "LoginRequired" in str(body["meta"].get("msg")):
                s.forget()
                raise SourceError("source.family.auth_failed", "session refused")
            return None
        return body

    def _read_classic(self, areas) -> dict:
        relogin = [True]
        out = {}
        for area in areas:
            body = self._classic(area, relogin)
            if body is not None:
                out[area] = body
        return out

    def _sign_out(self) -> None:
        s = self._session
        if not s or not s.signed_in:
            return
        try:
            self._t.request("POST", LOGOUT[s.style], {}, headers=s.headers())
        except Exception:
            pass
        finally:
            s.forget()

    # ------------------------------------------------------------------ official API
    def _official_base(self):
        if self._official_prefix is not None:
            return self._official_prefix
        found = False
        for prefix in ("/proxy/network", ""):
            status, body = self._get(prefix + "/integration/v1/info", official=True)
            if status == 200 and isinstance(body, dict):
                self._official_prefix = prefix
                found = True
                self._info = body
                break
            if status in (401, 403):
                raise SourceError("source.family.key_refused", "API key refused")
        if not found:
            self._official_prefix = False
        return self._official_prefix

    def _page(self, path: str) -> list:
        out = []
        for n in range(MAX_PAGES):
            status, body = self._get("%s?offset=%d&limit=%d" % (path, n * PAGE, PAGE), official=True)
            if status in (400, 404, 405):
                return None if n == 0 else out
            if status in (401, 403):
                raise SourceError("source.family.key_refused", "API key refused")
            if not 200 <= status < 300:
                raise SourceError("source.family.protocol", "unexpected status %d" % status)
            data = body.get("data") if isinstance(body, dict) else body
            if not isinstance(data, list):
                raise SourceError("source.family.protocol", "unexpected reply shape")
            out += data
            total = body.get("totalCount") if isinstance(body, dict) else None
            if not data or len(data) < PAGE or (isinstance(total, int) and len(out) >= total):
                break
        return out

    def list_sites(self) -> list:
        """The sites of this controller over the official API (API key): [{"ref", "name"}] (read only, M1)."""
        prefix = self._official_base() if self._key else False
        if prefix is False:
            raise SourceError("source.family.config", "the site list needs an API key")
        out = []
        for s in (self._page(prefix + "/integration/v1/sites") or [])[:64]:
            ref = s.get("internalReference") if isinstance(s, dict) else None
            if isinstance(ref, str) and SITE_RE.match(ref):
                name = s.get("name") if isinstance(s.get("name"), str) else ref
                out.append({"ref": ref, "name": name[:60]})
        return out

    def _read_official(self, light: bool) -> dict:
        prefix = self._official_base()
        if prefix is False:
            return {}
        base = prefix + "/integration/v1"
        if self._site_id is None:
            sites = self._page(base + "/sites") or []
            pick = [s for s in sites if isinstance(s, dict) and s.get("internalReference") == self._site] or \
                   ([s for s in sites if isinstance(s, dict)] if len(sites) == 1 else [])
            if not pick or not re.fullmatch(_ID, str(pick[0].get("id") or "")):
                raise SourceError("source.family.config", "site not found")
            self._site_id = pick[0]["id"]
        sb = "%s/sites/%s" % (base, self._site_id)
        out = {"info": getattr(self, "_info", None)}
        lists = {"o_clients": "clients"} if light else OFFICIAL_LISTS
        for key, rel in lists.items():
            got = self._page("%s/%s" % (sb, rel))
            if got is not None:
                out[key] = got
        details = {"o_devices": "devices"} if light else OFFICIAL_DETAILS
        budget = [MAX_DETAILS]
        for key, rel in details.items():
            overview = self._page("%s/%s" % (sb, rel))
            if overview is None:
                continue
            full = []
            for item in overview:
                oid = str((item or {}).get("id") or "") if isinstance(item, dict) else ""
                if not re.fullmatch(_ID, oid) or budget[0] <= 0:
                    full.append(item)
                    continue
                budget[0] -= 1
                status, body = self._get("%s/%s/%s" % (sb, rel, oid), official=True)
                full.append(body if status == 200 and isinstance(body, dict) else item)
            out[key] = full
        if not light:
            vpn = []
            for rel in ("vpn/servers", "vpn/site-to-site-tunnels"):
                vpn += self._page("%s/%s" % (sb, rel)) or []
            out["o_vpn"] = vpn
        return out

    # ------------------------------------------------------------------ polls
    def collect(self, light: bool = False) -> dict:
        """Blocking: every reply this poll could get (keys see ``model``). Call from a thread."""
        raw = {}
        if self._key:
            raw.update(self._read_official(light))
        if self._user:
            raw.update(self._read_classic(LIGHT_CLASSIC if light else CLASSIC_AREAS))
        self.last_raw_keys = tuple(sorted(k for k, v in raw.items() if v is not None))
        return raw

    def capabilities(self) -> dict:
        return self._caps

    def _refine(self, raw: dict, state: dict) -> dict:
        has_official = any(k.startswith("o_") for k in raw)
        has_classic = any(k in raw for k in CLASSIC_AREAS)
        missing = set()
        if not has_classic:
            missing |= {"port_profiles", "port_forwards", "dhcp_reservations"}
        if not has_official:
            missing |= {"acl"}
        if not state.get("zones"):
            missing |= {"zones", "policies"}
        if not state.get("ports"):
            missing |= {"ports"}
        caps = capabilities.refine(capabilities.validate(CAPABILITIES, self.family), version=state["controller"]["version"] or "",
                                   read_missing=missing, write_missing=READ_ONLY_WRITE_KEYS)
        caps["api"]["kind"] = "official-local" if has_official else "classic-local"
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
            try:
                last_full = None                  # the first poll is always a full one (monotonic time may start near 0)
                while True:
                    full = last_full is None or time.monotonic() - last_full >= self.full_interval
                    for ev in await self.poll(light=not full):
                        await emit_event(emit_, ev)
                    if full:
                        last_full = time.monotonic()
                    await status.set(emit_, True, "source.family.ok")
                    await asyncio.sleep(self.interval)
            finally:
                if self._session:
                    try:
                        await asyncio.wait_for(asyncio.to_thread(self._sign_out), 5.0)
                    except Exception:
                        self._session.forget()

        await run_with_backoff(SOURCE, MODULE, emit, one_session)

    async def test(self):
        async def probe():
            raw = await asyncio.to_thread(self.collect, False)
            st = model.to_neutral(raw)
            return {"version": st["controller"]["version"] or None, "networks": len(st["networks"]),
                    "zones": len(st["zones"]), "policies": len(st["policies"]), "devices": len(st["equipment"]),
                    "clients": len(st["devices"])}

        return await short_test(MODULE, probe(), self._t.timeout * 12 + 10)

    async def account_scope(self) -> str:
        """read_only | can_write | unknown. Checkable for the view-only account only (its role on the site); an API
        key does not tell its rights, so a key alone reports ``unknown``."""
        if not self._user:
            return "unknown"

        def check():
            body = self._classic_self()
            data = body.get("data") if isinstance(body, dict) else None
            me = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
            role = str(me.get("site_role") or "")
            if me.get("is_super"):
                return "can_write"
            if role == "readonly":         # the role on the site decides: a self-hosted controller may also report
                return "read_only"         # is_owner for a view-only account (#21)
            if me.get("is_owner") or role == "admin":
                return "can_write"
            return "unknown"

        return await asyncio.to_thread(check)

    def _classic_self(self):
        s = self._session
        relogin = [True]
        if not s.signed_in:
            self._sign_in()
        path = "%s/api/s/%s/self" % (PREFIX[s.style], self._site)
        status, body = self._get(path)
        if status == 401 and relogin[0]:
            self._sign_in()
            status, body = self._get(path)
        return body if status == 200 else None
