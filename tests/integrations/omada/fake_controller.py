"""A local fake of the controller's Open API (plain http on 127.0.0.1). Checks the token exchange and the token on every
data request, pages lists as documented, answers errors with HTTP 200 and an ``errorCode``, records every request
(never the secret) and can play: a refused exchange, an unknown controller ID, an expired token, a role that may not
read an area, areas an older release does not have, HTTP errors."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import fixtures as fx

LIST_PATHS = {"devices": "devices", "clients": "clients", "networks": "lan-networks", "profiles": "lan-profiles",
              "acl_gateway": "acls/osg-acls", "acl_switch": "acls/osw-acls", "acl_eap": "acls/eap-acls"}
SITE_RE = re.compile(r"\A/openapi/v1/%s/sites/%s/(.+)\Z" % (fx.CID, fx.SITE_ID))


class FakeController:
    def __init__(self, announce_id=True):
        self.data = fx.site_data()
        self.announce_id = announce_id
        self.mode = "ok"              # ok | bad_secret | wrong_id | http
        self.denied = set()           # path fragments answered with a permission error
        self.missing = set()          # path fragments answered with HTTP 404 (an older release)
        self.http_status = 500
        self.requests = []            # dicts: method, path, headers (without Authorization value), body
        self.exchanges = 0
        self.tokens = set()
        self.expire_next = False      # the next data request answers "token expired" once (and drops the token)
        self.page_cap = None          # serve at most this many rows per page whatever pageSize asks
        self.sites = list(fx.SITES)
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer._handle(self, "GET", b"")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer._handle(self, "POST", self.rfile.read(n))

            def do_PUT(self):
                outer._handle(self, "PUT", b"")

            do_DELETE = do_PATCH = do_PUT

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    # ------------------------------------------------------------------
    def _send(self, h, status, body):
        data = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def _handle(self, h, method, raw):
        parts = urlsplit(h.path)
        auth = h.headers.get("Authorization") or ""
        rec = {"method": method, "path": h.path, "body": raw, "has_auth": bool(auth), "auth_scheme": auth.split("=", 1)[0],
               "headers": {k: v for k, v in h.headers.items() if k.lower() != "authorization"}}
        with self._lock:
            self.requests.append(rec)
        if self.mode == "http":
            return self._send(h, self.http_status, b"<html>error</html>")
        if method != "GET" and not (method == "POST" and parts.path == "/openapi/authorize/token"):
            return self._send(h, 405, {})
        if parts.path == "/api/info":
            info = dict(fx.INFO)
            if not self.announce_id:
                info.pop("omadacId")
            return self._send(h, 200, fx.wrap(info))
        if parts.path == "/openapi/authorize/token":
            return self._token(h, parts, raw)
        token = auth.split("=", 1)[1] if auth.startswith("AccessToken=") else None
        if self.expire_next:
            self.expire_next = False
            self.tokens.clear()
        if token not in self.tokens:
            return self._send(h, 200, fx.wrap(None, -44112, "The access token has expired or is invalid."))
        return self._data(h, parts)

    def _token(self, h, parts, raw):
        self.exchanges += 1
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._send(h, 400, {})
        if parse_qs(parts.query).get("grant_type") != ["client_credentials"]:
            return self._send(h, 200, fx.wrap(None, -1001, "bad grant"))
        if self.mode == "wrong_id" or body.get("omadacId") != fx.CID:
            return self._send(h, 200, fx.wrap(None, -7131, "Controller ID not exist."))
        if self.mode == "bad_secret" or body.get("client_id") != fx.CLIENT_ID or body.get("client_secret") != fx.CLIENT_SECRET:
            return self._send(h, 200, fx.wrap(None, -44106, "Invalid client credentials."))
        tok = "AT-%032x" % (0xABCDEF0000 + self.exchanges)
        self.tokens.add(tok)
        return self._send(h, 200, fx.wrap({"accessToken": tok, "tokenType": "bearer", "expiresIn": 7199, "refreshToken": "RT-unused"}))

    def _data(self, h, parts):
        path = parts.path
        for frag in self.missing:
            if frag in path:
                return self._send(h, 404, {})
        for frag in self.denied:
            if frag in path:
                return self._send(h, 200, fx.wrap(None, -1505, "The current user does not have permission to access this resource."))
        q = parse_qs(parts.query)
        pg, size = int((q.get("page") or ["1"])[0]), int((q.get("pageSize") or ["100"])[0])
        if self.page_cap:
            size = min(size, self.page_cap)
        if path == "/openapi/v1/%s/sites" % fx.CID:
            return self._send(h, 200, fx.wrap(fx.page(self.sites, pg, size)))
        if not path.startswith("/openapi/v1/%s/sites/%s/" % (fx.CID, fx.SITE_ID)):
            return self._send(h, 200, fx.wrap(None, -7131, "Controller ID not exist."))
        rel = SITE_RE.match(path).group(1)
        for key, frag in LIST_PATHS.items():
            if rel == frag:
                return self._send(h, 200, fx.wrap(fx.page(self.data[key], pg, size)))
        if rel == "wireless-network/wlans":
            return self._send(h, 200, fx.wrap(self.data["wlans"]))
        m = re.fullmatch(r"wireless-network/wlans/([^/]+)/ssids", rel)
        if m:
            return self._send(h, 200, fx.wrap(fx.page(self.data["ssids"].get(m.group(1), []), pg, size)))
        m = re.fullmatch(r"gateways/([0-9A-Fa-f-]+)/wan-status", rel)
        if m:
            return self._send(h, 200, fx.wrap(self.data["wans"] if m.group(1).upper() == fx.GW else []))
        m = re.fullmatch(r"(switches|stat/switches)/([0-9A-Fa-f-]+)", rel)
        if m:
            src = self.data["switch_info" if m.group(1) == "switches" else "switch_stat"].get(m.group(2).upper())
            return self._send(h, 200, fx.wrap(src) if src is not None else fx.wrap(None, -1600, "Device not found."))
        return self._send(h, 404, {})

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)
