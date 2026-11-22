"""Read-only live source for a switch and access point controller (local JSON API).

Many home and small-office switch / access point product lines are managed by a local controller application.
With a local view-only account its API tells which client MAC sits on which switch port or access point. This
source logs in, reads two lists every 60 seconds and emits one ``topology`` event. It never changes anything.

Two API styles, auto-detected (``style``: ``auto`` | ``console`` | ``classic``, default ``auto``):

* console: ``POST /api/auth/login``; data below ``/proxy/network/api/s/<site>/stat/{sta,device}``;
  logout ``POST /api/auth/logout``;
* classic: ``POST /api/login``; data below ``/api/s/<site>/stat/{sta,device}``; logout ``POST /api/logout``.

``auto`` tries console first and falls back to classic on HTTP 401/404/405 (the credentials count as rejected
only when both ways refuse); the style that worked is remembered.
The login reply's cookies are kept (``name=value`` only) and sent back as one ``Cookie`` header; a CSRF token
(header ``X-CSRF-Token``, or a cookie ``csrf_token``) is sent back as ``X-CSRF-Token``.

Read-only guarantee: exactly one method performs HTTP requests (``Controller._send``) and it refuses everything
outside ``ALLOWED_POST`` (login/logout) and ``ALLOWED_GET`` (the two list endpoints, fixed templates), and any
POST body other than the login credentials. A static test checks this module's source.

Emits ``{"type": "topology", "source": "controller", "t", "devices": [...], "clients": [...]}``:

* device: ``mac``, ``ip``, ``name``, ``model`` from the same-named fields; ``role`` from ``type``
  (switch / ap / router / other); ``uplink`` from ``uplink`` (fallback ``last_uplink``): ``uplink_mac`` -> ``mac``,
  ``uplink_remote_port`` -> ``port`` (at the parent), ``port_idx`` -> ``own_port``, ``type`` wire/wireless ->
  ``medium`` wired/radio;
* client: ``mac``, ``ip``, ``name`` (or ``hostname``); ``is_wired`` true -> ``medium`` wired, ``parent_mac`` =
  ``sw_mac`` (else ``last_uplink_mac``), ``port`` = ``sw_port``; ``is_wired`` false -> ``medium`` radio,
  ``parent_mac`` = ``ap_mac``, ``band`` from ``radio`` (ng 2.4ghz, na 5ghz, 6e 6ghz; only when ``radio`` is
  missing the ``channel`` is used: up to 14 -> 2.4ghz, 36..165 -> 5ghz), ``ssid`` = ``essid``, ``signal`` = ``signal``
  (whole dBm, -100..-10, anything else ``None``; W2).
  Clients without a parent are skipped. Unknown values are ``None``, never guessed.

Password, cookies and CSRF token are kept in private attributes only; they never appear in logs, exception
texts, events or ``repr()``.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import re
import urllib.parse

from ._common import SourceError, StatusTracker, clip, emit_event, now, run_with_backoff, short_test
from ._ctlmap import BANDS, MAC_RE, ROLES, _unique, ip_str, map_client, map_device, name_str, norm_mac, port_str  # noqa: F401

MODULE = "controller"
SOURCE = "controller"

SITE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SITE = r"[A-Za-z0-9_-]{1,64}"
STYLES = ("auto", "console", "classic")
LOGIN_PATH = {"console": "/api/auth/login", "classic": "/api/login"}
LOGOUT_PATH = {"console": "/api/auth/logout", "classic": "/api/logout"}
DATA_PREFIX = {"console": "/proxy/network/api/s/%s/stat/", "classic": "/api/s/%s/stat/"}
KINDS = {"clients": "sta", "devices": "device"}
SITES_PATH = {"console": "/proxy/network/api/self/sites", "classic": "/api/self/sites"}   # the controller's site list (M1)
MAX_SITES = 64

# The complete allow-list. Everything else is refused before anything is sent.
ALLOWED_POST = frozenset(LOGIN_PATH.values()) | frozenset(LOGOUT_PATH.values())
ALLOWED_GET = (
    re.compile(r"\A/proxy/network/api/s/%s/stat/(?:sta|device)\Z" % _SITE),
    re.compile(r"\A/api/s/%s/stat/(?:sta|device)\Z" % _SITE),
    re.compile(r"\A(?:/proxy/network)?/api/self/sites\Z"),
)
LOGIN_FIELDS = frozenset({"username", "password"})

MAX_BYTES = 8 * 1024 * 1024
MAX_CLIENTS = 5000
MAX_DEVICES = 1000
MAX_COOKIES = 20
MAX_COOKIE_LEN = 4096



def _ssl_context(allow_self_signed: bool = False):
    import ssl
    ctx = ssl.create_default_context()
    if allow_self_signed:              # explicit opt-in only (a controller with its own certificate)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ForbiddenRequest(Exception):
    """A request outside the allow-list was about to be sent - refused, nothing was sent."""


class _Expired(Exception):
    """The session is no longer valid (HTTP 401 or a login-required reply)."""


def parse_url(url):
    """Controller address -> (tls, host, port). No credentials, path ignored."""
    text = str(url or "").strip()
    if not text:
        raise SourceError("source.controller.config", "controller URL missing")
    if "://" not in text:
        text = "https://" + text
    try:
        u = urllib.parse.urlsplit(text)
        port = u.port
    except ValueError:
        raise SourceError("source.controller.config", "controller URL invalid") from None
    scheme = u.scheme.lower()
    if scheme not in ("http", "https") or not u.hostname:
        raise SourceError("source.controller.config", "controller URL must be http(s)://")
    if u.username or u.password:
        raise SourceError("source.controller.config", "credentials in the URL are not allowed")
    tls = scheme == "https"
    return tls, u.hostname, port or (443 if tls else 80)


class Controller:
    """Live source: switch and access point controller (local JSON API), strictly read-only."""

    name = SOURCE

    def __init__(self, url, username, password, site="default", style="auto", allow_self_signed=False,
                 interval=60.0, timeout=10.0):
        self._tls, self._host, self._port = parse_url(url)
        user = username if isinstance(username, str) else ""
        pw = password if isinstance(password, str) else ""
        if not user or len(user) > 256 or any(ord(c) < 32 for c in user):
            raise SourceError("source.controller.config", "username missing or invalid")
        if not pw or len(pw) > 1024 or any(ord(c) < 32 for c in pw):
            raise SourceError("source.controller.config", "password missing or invalid")
        if not isinstance(site, str) or not SITE_RE.fullmatch(site):
            raise SourceError("source.controller.config", "site invalid")
        if style not in STYLES:
            raise SourceError("source.controller.config", "style invalid")
        self._user, self._password = user, pw
        self._site = site
        self._style_cfg = style
        self._style = None if style == "auto" else style      # resolved on the first successful login
        self._allow_self_signed = bool(allow_self_signed)
        self.interval = max(1.0, float(interval))
        self.timeout = max(1.0, float(timeout))
        self._cookies: dict[str, str] = {}
        self._csrf = None
        self._logged_in = False
        self.refused = 0

    def __repr__(self):
        host = self._host if ":" not in self._host else "[%s]" % self._host
        return "Controller(host=%r, site=%r)" % ("%s:%d" % (host, self._port), self._site)

    __str__ = __repr__

    # ------------------------------------------------------------ the single request path to the controller
    def _send(self, method, path, body=None):
        """Perform one allowed HTTP request (blocking; call it in a thread). Returns (status, headers, data)."""
        if method == "POST" and path in ALLOWED_POST:
            if path in LOGIN_PATH.values():
                if not isinstance(body, dict) or set(body) != LOGIN_FIELDS:
                    self.refused += 1
                    raise ForbiddenRequest("POST %s" % clip(path, 60))
            elif body is not None:
                self.refused += 1
                raise ForbiddenRequest("POST %s" % clip(path, 60))
        elif not (method == "GET" and body is None and isinstance(path, str)
                  and any(p.match(path) for p in ALLOWED_GET)):
            self.refused += 1
            raise ForbiddenRequest("%s %s" % (clip(method, 8), clip(path, 60)))
        headers = {"Accept": "application/json", "User-Agent": "lanloom", "Connection": "close"}
        payload = None
        if method == "POST":
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body if body is not None else {}).encode()
        if self._cookies:
            headers["Cookie"] = "; ".join("%s=%s" % kv for kv in self._cookies.items())
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        if self._tls:
            conn = http.client.HTTPSConnection(self._host, self._port, timeout=self.timeout,
                                               context=_ssl_context(self._allow_self_signed))
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)
        try:
            try:
                conn.request(method, path, body=payload, headers=headers)
                resp = conn.getresponse()
                data = resp.read(MAX_BYTES + 1)
                heads = resp.getheaders()
                status = resp.status
            except http.client.HTTPException:
                raise SourceError("source.controller.protocol", "malformed HTTP reply") from None
        finally:
            conn.close()
        if len(data) > MAX_BYTES:
            raise SourceError("source.controller.protocol", "reply too large")
        return status, heads, data

    async def _call(self, method, path, body=None):
        status, heads, data = await asyncio.to_thread(self._send, method, path, body)
        self._absorb(heads)
        return status, data

    def _absorb(self, heads):
        """Keep ``name=value`` of every Set-Cookie and the CSRF token; nothing else."""
        for key, value in heads:
            k = key.lower()
            if k == "set-cookie":
                pair = value.split(";", 1)[0].strip()
                name, sep, val = pair.partition("=")
                if (not sep or not name or len(pair) > MAX_COOKIE_LEN
                        or any(ord(c) < 33 or ord(c) > 126 or c in ";," for c in name + val)):
                    continue
                if not val:
                    self._cookies.pop(name, None)
                elif name in self._cookies or len(self._cookies) < MAX_COOKIES:
                    self._cookies[name] = val
            elif k in ("x-csrf-token", "x-updated-csrf-token"):
                if value and len(value) <= 2048 and all(32 < ord(c) < 127 for c in value):
                    self._csrf = value
        if not self._csrf:                                  # classic style keeps it in a cookie
            self._csrf = self._cookies.get("csrf_token")

    # ------------------------------------------------------------ session
    def _forget(self):
        self._cookies.clear()
        self._csrf = None
        self._logged_in = False

    @staticmethod
    def _json(data):
        try:
            return json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise SourceError("source.controller.protocol", "reply is not JSON") from None

    async def _login(self):
        self._forget()
        order = [self._style] if self._style else ["console", "classic"]
        creds = {"username": self._user, "password": self._password}
        refused = False
        for i, style in enumerate(order):
            status, data = await self._call("POST", LOGIN_PATH[style], creds)
            more = i + 1 < len(order)
            if more and status in (401, 404, 405):
                refused = refused or status == 401           # a self-hosted controller answers the console path with 401
                continue
            if status in (400, 401, 403):
                raise SourceError("source.controller.auth_failed", "login refused")
            if refused and status in (404, 405):             # the console path refused, the classic path does not exist
                raise SourceError("source.controller.auth_failed", "login refused")
            if not 200 <= status < 300:
                raise SourceError("source.controller.protocol", "unexpected login status %d" % status)
            obj = self._json(data) if data.strip() else {}
            meta = obj.get("meta") if isinstance(obj, dict) else None
            if isinstance(meta, dict) and meta.get("rc") not in (None, "ok"):
                raise SourceError("source.controller.auth_failed", "login refused")
            if not self._cookies:
                raise SourceError("source.controller.protocol", "login gave no session")
            self._style = style
            self._logged_in = True
            return
        raise SourceError("source.controller.protocol", "no known login endpoint")

    async def _logout(self):
        """Best-effort end of the session."""
        if not self._logged_in or self._style is None:
            return
        try:
            await self._call("POST", LOGOUT_PATH[self._style])
        except Exception:
            pass
        finally:
            self._forget()

    async def close(self):
        await self._logout()

    async def _fetch(self, kind):
        path = (DATA_PREFIX[self._style] % self._site) + KINDS[kind]
        status, data = await self._call("GET", path)
        if status == 401:
            raise _Expired()
        if status == 403:
            raise SourceError("source.controller.forbidden", "account may not read this")
        if not 200 <= status < 300:
            raise SourceError("source.controller.protocol", "unexpected status %d" % status)
        obj = self._json(data)
        if isinstance(obj, dict):
            meta = obj.get("meta")
            if isinstance(meta, dict) and meta.get("rc") not in (None, "ok"):
                if "LoginRequired" in str(meta.get("msg")):
                    raise _Expired()
                raise SourceError("source.controller.protocol", "controller reported an error")
            obj = obj.get("data")
        if not isinstance(obj, list):
            raise SourceError("source.controller.protocol", "unexpected reply shape")
        return obj

    async def _poll_once(self):
        sta = await self._fetch("clients")
        dev = await self._fetch("devices")
        return sta, dev

    async def poll(self):
        """Log in when needed, read both lists once, return the topology event."""
        try:
            if not self._logged_in:
                await self._login()
            try:
                sta, dev = await self._poll_once()
            except _Expired:
                await self._login()                     # one re-login per poll
                try:
                    sta, dev = await self._poll_once()
                except _Expired:
                    raise SourceError("source.controller.auth_failed", "session refused") from None
        except BaseException:
            self._logged_in = False
            raise
        return {"type": "topology", "source": SOURCE, "t": now(),
                "devices": _unique((map_device(d) for d in dev), MAX_DEVICES),
                "clients": _unique((map_client(c) for c in sta[:MAX_CLIENTS * 2]), MAX_CLIENTS)}

    async def sites(self):
        """The sites this controller manages (read only): [{"ref": site name for the integration, "name": label}].
        A further Lanloom site (M1) can read one of them with the same account."""
        async def probe():
            try:
                if not self._logged_in:
                    await self._login()
                status, data = await self._call("GET", SITES_PATH[self._style])
                if status in (401, 403):
                    raise SourceError("source.controller.forbidden", "account may not read the site list")
                if not 200 <= status < 300:
                    raise SourceError("source.controller.protocol", "unexpected status %d" % status)
                obj = self._json(data)
                rows = obj.get("data") if isinstance(obj, dict) else obj
                if not isinstance(rows, list):
                    raise SourceError("source.controller.protocol", "unexpected reply shape")
                out = []
                for r in rows[:MAX_SITES]:
                    ref = r.get("name") if isinstance(r, dict) else None
                    if isinstance(ref, str) and SITE_RE.fullmatch(ref):
                        label = r.get("desc") if isinstance(r.get("desc"), str) else ref
                        out.append({"ref": ref, "name": clip(label, 60)})
                return out
            finally:
                await self._logout()

        return await short_test(MODULE, probe(), self.timeout * 4 + 5)

    # ------------------------------------------------------------ source contract
    async def run(self, emit):
        async def session(emit_, status: StatusTracker):
            try:
                while True:
                    event = await self.poll()
                    await emit_event(emit_, event)
                    await status.set(emit_, True, "source.controller.ok")
                    await asyncio.sleep(self.interval)
            finally:
                try:
                    await asyncio.wait_for(self._logout(), 3.0)
                except Exception:
                    self._forget()

        await run_with_backoff(SOURCE, MODULE, emit, session)

    async def test(self):
        async def probe():
            try:
                event = await self.poll()
            finally:
                await self._logout()
            return {"clients": len(event["clients"]), "devices": len(event["devices"])}

        return await short_test(MODULE, probe(), self.timeout * 6 + 5)
