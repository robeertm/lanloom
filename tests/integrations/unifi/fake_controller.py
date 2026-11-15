"""A local fake of a controller that serves both APIs over plain HTTP on 127.0.0.1 (no real network).

``style="console"``: console layout (``/proxy/network`` prefix, sign-in ``/api/auth/login`` with a TOKEN cookie and
an X-CSRF-Token header). ``style="classic"``: self-hosted layout (no prefix, sign-in ``/api/login``, cookies only).
Every request is recorded; official lists are paged by the offset and limit of the query string."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import fixtures as fx

USER = "viewer"
PW = "pw-for-tests"          # secret-scan: fake
KEY = "key-for-tests"          # secret-scan: fake
CSRF = "csrf-for-tests"

CLASSIC_PATHS = {"self": "self", "sysinfo": "stat/sysinfo", "device": "stat/device", "sta": "stat/sta",
                 "networkconf": "rest/networkconf", "wlanconf": "rest/wlanconf", "portconf": "rest/portconf",
                 "portforward": "rest/portforward", "user": "rest/user", "firewallgroup": "rest/firewallgroup",
                 "firewallrule": "rest/firewallrule"}
V2_PATHS = {"zone": "firewall/zone", "policies": "firewall-policies"}
OFFICIAL_LISTS = {"networks": "o_networks", "firewall/zones": "o_zones", "firewall/policies": "o_policies", "wifi/broadcasts": "o_wifi",
                  "devices": "o_devices", "clients": "o_clients", "wans": "o_wans", "vpn/servers": "o_vpn", "vpn/site-to-site-tunnels": None,
                  "acl-rules": "o_acl", "dns/policies": "o_dns"}
DETAILS = ("networks", "devices", "wifi/broadcasts")


class FakeController:
    def __init__(self, style="console", classic=True, official=True, data=None):
        self.style = style
        self.has_classic, self.has_official = classic, official
        data = data if data is not None else fx.both()
        self.classic = {k: v for k, v in data.items() if not (k.startswith("o_") or k == "info")}
        self.official = {k: v for k, v in data.items() if k.startswith("o_") or k == "info"}
        self.sites = [{"id": fx.SITE_ID, "internalReference": "default", "name": "Default"}]
        self.self_reply = fx.classic_wrap([{"name": "viewer", "site_role": "readonly", "is_super": False}])
        self.requests = []             # (method, path with query, lower-case headers, body)
        self.sessions = set()
        self.logins = 0
        self.fail = {}                 # path regex -> status
        self.expire_next = 0           # answer the next N data requests of a classic session with 401 and drop the session
        self.always_401 = False        # every classic data request is answered with 401 (sessions never hold)
        self.login_refused = 0         # status of every sign-in (0 = work normally)
        self.page_log = []             # (path, offset, limit) of every paged official request
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    @property
    def prefix(self):
        return "/proxy/network" if self.style == "console" else ""

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def methods(self):
        return {r[0] for r in self.requests}

    def paths(self, method="GET"):
        return [r[1].split("?")[0] for r in self.requests if r[0] == method]

    def count(self, method, rx):
        return sum(1 for r in self.requests if r[0] == method and re.search(rx, r[1]))

    # ------------------------------------------------------------------ routing
    def classic_reply(self, path):
        base = self.prefix + "/api/s/default/"
        v2 = self.prefix + "/v2/api/site/default/"
        if path.startswith(base):
            rel = path[len(base):]
            for key, p in CLASSIC_PATHS.items():
                if rel == p:
                    return self.self_reply if key == "self" else self.classic.get(key)
        if path.startswith(v2):
            rel = path[len(v2):]
            for key, p in V2_PATHS.items():
                if rel == p:
                    return self.classic.get(key)
        return None

    def official_reply(self, rel, query):
        if rel == "info":
            return self.official.get("info")
        if rel == "sites":
            return self.page(self.sites, query, "/sites")
        m = re.fullmatch(r"sites/([A-Za-z0-9-]+)/(.+)", rel)
        if not m:
            return None
        area = m.group(2)
        if area in OFFICIAL_LISTS:
            key = OFFICIAL_LISTS[area]
            rows = self.official.get(key) if key else []
            if rows is None:
                return None
            if area in DETAILS:
                rows = [{"id": r["id"], "name": r.get("name")} for r in rows]       # the overview has no details
            elif area == "vpn/servers":
                rows = [r for r in rows if r.get("type") != "SITE_TO_SITE"]
            return self.page(rows, query, "/" + area)
        for det in DETAILS:
            dm = re.fullmatch(re.escape(det) + r"/([A-Za-z0-9-]+)", area)
            if dm:
                return next((r for r in self.official[OFFICIAL_LISTS[det]] if r["id"] == dm.group(1)), None)
        return None

    def page(self, rows, query, name):
        offset = int(query.get("offset", ["0"])[0])
        limit = int(query.get("limit", ["25"])[0])
        self.page_log.append((name, offset, limit))
        chunk = rows[offset:offset + limit]
        return {"offset": offset, "limit": limit, "count": len(chunk), "totalCount": len(rows), "data": chunk}

    # ------------------------------------------------------------------ server
    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, obj=None, headers=()):
                body = json.dumps(obj if obj is not None else {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def record(self, body=None):
                with fake._lock:
                    fake.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                self.record(body)
                path = self.path
                if path in ("/api/auth/logout", "/api/logout"):
                    fake.sessions.clear()
                    return self.reply(200)
                login = "/api/auth/login" if fake.style == "console" else "/api/login"
                if path != login or not fake.has_classic:
                    return self.reply(404)
                if fake.login_refused:
                    return self.reply(fake.login_refused, {"meta": {"rc": "error"}})
                if body.get("username") != USER or body.get("password") != PW:
                    return self.reply(401 if fake.style == "console" else 400, {"meta": {"rc": "error"}})
                with fake._lock:
                    fake.logins += 1
                    sid = "sid%d" % fake.logins
                    fake.sessions.add(sid)
                if fake.style == "console":
                    heads = [("Set-Cookie", "TOKEN=%s; path=/; samesite=none; secure; httponly; Partitioned" % sid), ("X-CSRF-Token", CSRF)]
                else:
                    heads = [("Set-Cookie", "unifises=%s; Path=/; HttpOnly" % sid), ("Set-Cookie", "csrf_token=%s; Path=/" % CSRF)]
                self.reply(200, {"meta": {"rc": "ok"}}, heads)

            def do_GET(self):
                self.record()
                parts = urlsplit(self.path)
                path, query = parts.path, parse_qs(parts.query)
                for rx, status in fake.fail.items():
                    if re.search(rx, path):
                        return self.reply(status, {"meta": {"rc": "error", "msg": "api.err.Invalid"}})
                om = re.fullmatch(r"(/proxy/network)?/integration/v1/(.*)", path)
                if om:
                    if (om.group(1) or "") != fake.prefix or not fake.has_official:
                        return self.reply(404)
                    if self.headers.get("X-API-KEY") != KEY:
                        return self.reply(401, {"statusCode": 401, "message": "Unauthorized"})
                    got = fake.official_reply(om.group(2), query)
                    return self.reply(200, got) if got is not None else self.reply(404, {"statusCode": 404})
                if not fake.has_classic:
                    return self.reply(404)
                cookie = self.headers.get("Cookie") or ""
                sid = next((c.split("=", 1)[1] for c in cookie.split("; ") if c.split("=", 1)[0] in ("TOKEN", "unifises")), None)
                if fake.always_401 or fake.expire_next > 0 or sid not in fake.sessions:
                    with fake._lock:
                        if fake.expire_next > 0:
                            fake.expire_next -= 1
                        fake.sessions.clear()
                    return self.reply(401, {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}})
                got = fake.classic_reply(path)
                return self.reply(200, got) if got is not None else self.reply(404, {"meta": {"rc": "error"}})

            def do_PUT(self):
                self.record()
                self.reply(405)

            do_DELETE = do_PATCH = do_PUT

        return H
