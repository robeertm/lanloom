"""Read half of the Omada family: reads the controller through its Open API, never writes.

Every request goes through ``transport.Transport`` with a *read* allow-list: GET on the paths below, plus the one POST
that exchanges the application's client ID and secret for an access token (its body must be exactly the controller
ID, the client ID and the secret, its path exactly the client-credentials exchange). Nothing else can be sent - no
change, no restart, no firmware action.

The access token (valid for two hours) is kept in memory, shared by every reader object of this process for the same
controller and application, and renewed shortly before it ends or after the controller refused it once per poll.
Token exchanges are capped per hour, and a refused exchange pauses further attempts for 15 minutes, so a wrong secret
can never lock anything through a flood of attempts.

The controller answers errors with HTTP 200 and an ``errorCode`` in the body; ``errorCode`` 0 is success. The read
half understands: a refused exchange or token (sign-in problem), a controller ID the controller does not know, a role
that may not read an area (the area is left out, the others still show), and areas an older release does not have.

Cadence: the light poll (devices and clients) every 60 s, the full poll (networks, port profiles, wireless networks,
access control rules, gateway internet connections, switch ports) every 5 minutes. Events:

* ``{"type": "topology", "source": "controller", ...}`` - same shape as the generic controller source, so the topology
  resolver merges it with manual links, LLDP and address tables;
* ``{"type": "config", "source": "family", "family": "omada", "state": <neutral model v2>, "capabilities": ...}`` -
  what the firewall tab and the inventory show (read only).

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
from ...sources._ctlmap import norm_mac

MODULE = "family"
SOURCE = "family"
URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}(?::\d{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](?::\d{1,5})?)/?\Z", re.I)
ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
CLIENT_ID_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,128}\Z")
LIGHT_S = 60.0
FULL_S = 300.0
PAGE = 100
MAX_PAGES = 50
MAX_SWITCHES = 24
MAX_GATEWAYS = 2
MAX_WLANS = 12
LOGINS_PER_HOUR = 6
PAUSE_AFTER_REFUSAL_S = 900.0
TOKEN_SLACK_S = 120.0

_ID = r"[A-Za-z0-9_-]{1,64}"
_MAC = r"[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5}"
_Q = r"(?:\?page=\d{1,4}&pageSize=\d{1,4})?"
_SITE = r"/openapi/v1/%s/sites/%s" % (_ID, _ID)
TOKEN_PATH = "/openapi/authorize/token?grant_type=client_credentials"
INFO_PATH = "/api/info"
GET_PATHS = (
    INFO_PATH,
    r"/openapi/v1/%s/sites%s" % (_ID, _Q),
    _SITE + r"/(?:devices|clients|lan-networks|lan-profiles|acls/osw-acls|acls/osg-acls|acls/eap-acls|wireless-network/wlans)%s" % _Q,
    _SITE + r"/wireless-network/wlans/%s/ssids%s" % (_ID, _Q),
    _SITE + r"/gateways/%s/wan-status" % _MAC,
    _SITE + r"/(?:switches|stat/switches)/%s" % _MAC,
)
ALLOW = read_allowlist(GET_PATHS, login=(re.escape(TOKEN_PATH), ("omadacId", "client_id", "client_secret")))

LIST_AREAS = {"devices": "devices", "clients": "clients", "networks": "lan-networks", "profiles": "lan-profiles",
              "acl_gateway": "acls/osg-acls", "acl_switch": "acls/osw-acls", "acl_eap": "acls/eap-acls"}
LIGHT_AREAS = ("devices", "clients")
DENIED = object()                  # the role may not read this area

_LOCK = threading.Lock()
_TOKENS = {}                       # key -> (Secret, valid until)
_LEDGER = {}                       # key -> ([exchange times], pause-until)


def _secret(value) -> Secret:
    return value if isinstance(value, Secret) else Secret(value)


def _key(host: str, client_id: str, secret: str) -> str:
    return hashlib.sha256(("%s\0%s\0%s" % (host, client_id, secret)).encode("utf-8")).hexdigest()


def _reset_for_tests() -> None:
    with _LOCK:
        _TOKENS.clear()
        _LEDGER.clear()


def _mac_path(mac) -> str:
    return norm_mac(mac).upper().replace(":", "-")


def _token_problem(body) -> bool:
    msg = str(body.get("msg") or "").lower()
    return "token" in msg or "expire" in msg


def _perm_problem(code, body) -> bool:
    msg = str(body.get("msg") or "").lower()
    return code == -1505 or "permission" in msg or "not allowed" in msg or "forbidden" in msg or "privilege" in msg


class OmadaReader:
    family = "omada"
    name = SOURCE

    def __init__(self, url=None, controller_id=None, client_id=None, client_secret=None, site=None,
                 allow_self_signed=False, interval=LIGHT_S, full_interval=FULL_S, timeout=15.0):
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise SourceError("source.family.config", "address missing or invalid")
        self._base = "%s://%s" % (m.group(1).lower(), m.group(2))
        self._host = m.group(2)
        cid = str(controller_id or "").strip()
        if cid and not ID_RE.match(cid):
            raise SourceError("source.family.config", "controller ID invalid")
        self._cid = cid or None
        self._client_id = str(client_id or "").strip()
        self._secret = _secret(client_secret)
        if not self._client_id or not self._secret:
            raise SourceError("source.family.config", "client ID and client secret are needed")
        if not CLIENT_ID_RE.match(self._client_id):
            raise SourceError("source.family.config", "client ID invalid")
        site = "".join(ch for ch in str(site or "").strip() if ch >= " ")[:64].strip()
        self._site_name = site or None
        self._site_id = None
        self._site = None
        self._t = Transport(self._base, ALLOW, allow_self_signed=bool(allow_self_signed), timeout=timeout)
        self._tkey = _key(self._host, self._client_id, self._secret.reveal())
        self.interval = max(5.0, float(interval))
        self.full_interval = max(self.interval, float(full_interval))
        self._info = None
        self._info_ok = False
        self._caps = capabilities.validate(CAPABILITIES, self.family)
        self.last_areas = ()
        self.last_denied = ()

    def __repr__(self):
        return "OmadaReader(%s)" % self._host

    __str__ = __repr__

    # ------------------------------------------------------------------ one request
    def _send(self, method: str, path: str, body=None, token=None):
        heads = {"Authorization": "AccessToken=%s" % token.reveal()} if token else None
        try:
            status, _, data = self._t.request(method, path, body, headers=heads)
        except Forbidden:
            raise SourceError("source.family.refused", "request outside the allow-list") from None
        except TransportError as exc:
            key = "source.family.unreachable" if exc.key.endswith("unreachable") else \
                "source.family.tls" if exc.key.endswith("certificate") else "source.family.protocol"
            raise SourceError(key) from None
        parsed = None
        if data:
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                if 200 <= status < 300:
                    raise SourceError("source.family.protocol", "reply is not JSON") from None
        return status, parsed

    # ------------------------------------------------------------------ controller info and token
    def _controller_info(self) -> dict:
        if self._info is not None:
            return self._info
        status, body = self._send("GET", INFO_PATH)
        res = body.get("result") if isinstance(body, dict) and body.get("errorCode") in (0, None) else None
        self._info = res if isinstance(res, dict) else {}
        self._info_ok = 200 <= status < 300
        if not self._cid and ID_RE.match(str(self._info.get("omadacId") or "")):
            self._cid = self._info["omadacId"]
        return self._info

    def _omadac(self) -> str:
        if not self._cid:
            self._controller_info()
        if not self._cid:
            if not self._info_ok:
                raise SourceError("source.family.protocol", "the controller did not answer its info request")
            raise SourceError("source.family.config", "controller ID missing and not announced by the controller")
        return self._cid

    def _exchange(self) -> Secret:
        t = time.time()
        with _LOCK:
            times, pause = _LEDGER.get(self._tkey, ([], 0.0))
            times = [x for x in times if t - x < 3600]
            if t < pause or len(times) >= LOGINS_PER_HOUR:
                _LEDGER[self._tkey] = (times, pause)
                raise SourceError("source.family.paused", "sign-in paused")
            times.append(t)
            _LEDGER[self._tkey] = (times, pause)

        def refused():
            with _LOCK:
                ts, _ = _LEDGER.get(self._tkey, ([], 0.0))
                _LEDGER[self._tkey] = (ts, time.time() + PAUSE_AFTER_REFUSAL_S)

        creds = {"omadacId": self._omadac(), "client_id": self._client_id, "client_secret": self._secret.reveal()}
        status, body = self._send("POST", TOKEN_PATH, creds)
        code = body.get("errorCode") if isinstance(body, dict) else None
        if code == -7131:
            raise SourceError("source.family.controller_id", "controller ID not known")
        if status in (400, 401, 403) or (isinstance(body, dict) and code not in (0, None)):
            refused()
            raise SourceError("source.family.auth_failed", "token exchange refused")
        res = body.get("result") if isinstance(body, dict) else None
        tok = res.get("accessToken") if isinstance(res, dict) else None
        if not 200 <= status < 300 or not isinstance(tok, str) or not 8 <= len(tok) <= 512 or not tok.isprintable() or " " in tok:
            raise SourceError("source.family.protocol", "token exchange gave no token")
        life = res.get("expiresIn")
        life = float(life) if isinstance(life, (int, float)) and not isinstance(life, bool) and life > 0 else 7200.0
        secret = Secret(tok)
        with _LOCK:
            _TOKENS[self._tkey] = (secret, time.time() + max(30.0, life - TOKEN_SLACK_S))
        return secret

    def _token(self) -> Secret:
        with _LOCK:
            tok = _TOKENS.get(self._tkey)
        if tok and time.time() < tok[1]:
            return tok[0]
        return self._exchange()

    def _forget_token(self) -> None:
        with _LOCK:
            _TOKENS.pop(self._tkey, None)

    # ------------------------------------------------------------------ data requests
    def _call(self, path: str, relogin: list):
        """``result`` of one data request; None when this release or role does not offer it; DENIED when the role may
        not read it. Raises SourceError for sign-in, controller ID and protocol problems."""
        token = self._token()
        status, body = self._send("GET", path, token=token)
        code = body.get("errorCode") if isinstance(body, dict) else None
        if status == 401 or (isinstance(body, dict) and code not in (0, None) and _token_problem(body)):
            if not relogin[0]:
                self._forget_token()
                raise SourceError("source.family.auth_failed", "token refused")
            relogin[0] = False
            self._forget_token()
            token = self._token()
            status, body = self._send("GET", path, token=token)
            code = body.get("errorCode") if isinstance(body, dict) else None
            if status == 401 or (isinstance(body, dict) and code not in (0, None) and _token_problem(body)):
                self._forget_token()
                raise SourceError("source.family.auth_failed", "token refused")
        if status == 403:
            return DENIED
        if status in (400, 404, 405):
            return None
        if not 200 <= status < 300:
            raise SourceError("source.family.protocol", "unexpected status %d" % status)
        if not isinstance(body, dict):
            raise SourceError("source.family.protocol", "unexpected reply shape")
        if code in (0, None):
            return body.get("result") if "result" in body else None
        if code == -7131:
            raise SourceError("source.family.controller_id", "controller ID not known")
        if _perm_problem(code, body):
            return DENIED
        return None

    def _pages(self, base: str, relogin: list):
        """All rows of a paged list (``result.data``), None/DENIED as for ``_call`` when the first page already fails."""
        out = []
        for n in range(1, MAX_PAGES + 1):
            res = self._call("%s?page=%d&pageSize=%d" % (base, n, PAGE), relogin)
            if res is None or res is DENIED:
                return res if n == 1 else out
            data = res.get("data") if isinstance(res, dict) else res
            if not isinstance(data, list):
                raise SourceError("source.family.protocol", "unexpected reply shape")
            out += [r for r in data if isinstance(r, dict)]
            total = res.get("totalRows") if isinstance(res, dict) else None
            # a controller may serve fewer rows per page than asked: only the total (or an empty page) ends the list
            if not data or (isinstance(total, int) and not isinstance(total, bool) and len(out) >= total) \
                    or (not isinstance(total, int) and len(data) < PAGE):
                break
        return out

    def _pick_site(self, relogin: list) -> dict:
        if self._site is not None:
            return self._site
        rows = self._pages("/openapi/v1/%s/sites" % self._omadac(), relogin)
        if rows is DENIED:
            raise SourceError("source.family.forbidden", "the application may not list sites")
        rows = [r for r in rows or [] if re.fullmatch(_ID, str(r.get("siteId") or ""))]
        pick = []
        if self._site_name:
            pick = [r for r in rows if str(r.get("name") or "").lower() == self._site_name.lower()]
        else:
            pick = [r for r in rows if r.get("primary")] or (rows if len(rows) == 1 else [])
        if not pick:
            raise SourceError("source.family.config", "site not found")
        self._site = pick[0]
        self._site_id = pick[0]["siteId"]
        return self._site

    # ------------------------------------------------------------------ polls
    def collect(self, light: bool = False) -> dict:
        """Blocking: every reply this poll could get (keys see ``model``). Call from a thread."""
        relogin = [True]
        raw = {}
        denied = set()
        site = self._pick_site(relogin)
        raw["site"] = {"siteId": site.get("siteId"), "name": site.get("name")}
        try:
            raw["info"] = self._controller_info()
        except SourceError:
            raw["info"] = {}
        sb = "/openapi/v1/%s/sites/%s" % (self._omadac(), self._site_id)
        for key in (LIGHT_AREAS if light else LIST_AREAS):
            got = self._pages("%s/%s" % (sb, LIST_AREAS[key]), relogin)
            if got is DENIED:
                denied.add(key)
            elif got is not None:
                raw[key] = got
        if not light:
            self._read_extras(sb, raw, relogin, denied)
        if "devices" in denied or ("devices" not in raw and "clients" in denied):
            raise SourceError("source.family.forbidden", "the application's role may not read devices")
        self.last_areas = tuple(sorted(k for k, v in raw.items() if v is not None))
        self.last_denied = tuple(sorted(denied))
        return raw

    def _read_extras(self, sb: str, raw: dict, relogin: list, denied: set) -> None:
        devs = raw.get("devices") or []
        gateways = [d for d in devs if str(d.get("type") or "").lower() == "gateway" and norm_mac(d.get("mac"))][:MAX_GATEWAYS]
        wans, answered = [], False
        for g in gateways:
            res = self._call("%s/gateways/%s/wan-status" % (sb, _mac_path(g["mac"])), relogin)
            if res is DENIED:
                denied.add("wans")
            elif isinstance(res, list):
                answered = True
                wans += [dict(w, gateway=norm_mac(g["mac"])) for w in res if isinstance(w, dict)]
        if answered and "wans" not in denied:
            raw["wans"] = wans
        switches = [d for d in devs if str(d.get("type") or "").lower() == "switch" and norm_mac(d.get("mac"))
                    and model.num(d.get("status")) == model.ONLINE][:MAX_SWITCHES]
        info, stat = {}, {}
        for s in switches:
            mac = norm_mac(s["mac"])
            for store, rel in ((info, "switches"), (stat, "stat/switches")):
                res = self._call("%s/%s/%s" % (sb, rel, _mac_path(mac)), relogin)
                if res is DENIED:
                    denied.add("ports")
                elif isinstance(res, dict):
                    store[mac] = res
        if (info or stat) and "ports" not in denied:
            raw["switch_info"], raw["switch_stat"] = info, stat
        groups = self._call("%s/wireless-network/wlans" % sb, relogin)
        if groups is DENIED:
            denied.add("ssids")
        elif isinstance(groups, list):
            ssids = []
            for g in [g for g in groups if isinstance(g, dict) and re.fullmatch(_ID, str(g.get("wlanId") or g.get("id") or ""))][:MAX_WLANS]:
                wid = g.get("wlanId") or g.get("id")
                rows = self._pages("%s/wireless-network/wlans/%s/ssids" % (sb, wid), relogin)
                if rows is DENIED:
                    denied.add("ssids")
                    break
                ssids += [dict(r, wlan=g.get("name") or wid) for r in rows or []]
            if "ssids" not in denied:
                raw["ssids"] = ssids

    def capabilities(self) -> dict:
        return self._caps

    def _refine(self, raw: dict, state: dict) -> dict:
        missing = set()
        for area, keys in (("networks", ("networks",)), ("port_profiles", ("profiles",)), ("ssids", ("ssids",)),
                           ("wans", ("wans",)), ("acl", ("acl_gateway", "acl_switch", "acl_eap")),
                           ("ports", ("switch_stat", "switch_info"))):
            if not any(k in raw for k in keys):
                missing.add(area)
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
            return {"version": st["controller"]["version"] or None, "site": st["controller"]["site"],
                    "devices": len(st["equipment"]), "clients": len(st["devices"]), "networks": len(st["networks"]),
                    "ssids": len(st["ssids"]), "ports": len(st["ports"])}

        return await short_test(MODULE, probe(), self._t.timeout * 30 + 10)

    async def account_scope(self) -> str:
        """The Open API does not tell the role of an application, so this reports ``unknown``; the guide asks for a
        role that only views (the transport cannot send a change either way)."""
        return "unknown"
