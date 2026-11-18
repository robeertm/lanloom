"""Local fakes of the two filters' APIs (plain http on 127.0.0.1). They record every request and can play refusals."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import fixtures as fx

PASSWORD = "s3cret"           # test values only
USER = "lanloom-read"
SID = "sid-test-0001"


class _Fake:
    def __init__(self):
        self.requests = []         # dicts: method, path, headers, body
        self.mode = "ok"
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _read(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_GET(self):
                outer._record(self, "GET", b"")
                outer.handle(self, "GET", b"")

            def do_POST(self):
                body = self._read()
                outer._record(self, "POST", body)
                outer.handle(self, "POST", body)

            def do_PUT(self):
                outer._record(self, "PUT", self._read())
                self._send(405, {})

            do_DELETE = do_PUT

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def _record(self, h, method, body):
        with self._lock:
            self.requests.append({"method": method, "path": h.path, "headers": {k.lower(): v for k, v in h.headers.items()},
                                  "body": body.decode("utf-8", "replace")})

    def paths(self):
        return [r["path"] for r in self.requests]

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(2)


class FakePihole(_Fake):
    def __init__(self, password=PASSWORD, data=None):
        self.password, self.data = password, dict(data or fx.DATA)
        self.logins = 0
        self.sid = None
        super().__init__()

    def handle(self, h, method, body):
        path = urlsplit(h.path).path
        if method == "POST" and path == "/api/auth":
            self.logins += 1
            if self.mode == "limit":
                return h._send(429, {"error": {"key": "rate_limiting"}})
            if json.loads(body or b"{}").get("password") != self.password:
                return h._send(401, {"session": {"valid": False, "sid": None}})
            self.sid = SID
            return h._send(200, {"session": {"valid": True, "sid": SID, "validity": 1800}})
        if self.mode == "expire":                         # the session ends: the next request is refused once
            self.mode, self.sid = "ok", None
        if self.password and h.headers.get("X-FTL-SID") != self.sid:
            return h._send(401, {"error": {"key": "unauthorized"}})
        d = self.data
        table = {"/api/stats/summary": d["summary"], "/api/history": d["history"], "/api/dns/blocking": d["blocking"],
                 "/api/lists": d["lists"], "/api/info/version": d["version"], "/api/stats/upstreams": d["upstreams"]}
        if path == "/api/stats/top_domains":
            return h._send(200, d["top_blocked"])
        if path == "/api/stats/top_clients":
            return h._send(200, d["clients_blocked"] if "blocked=true" in h.path else d["clients"])
        if path in table:
            return h._send(200, table[path])
        return h._send(404, {"error": {"key": "not_found"}})
