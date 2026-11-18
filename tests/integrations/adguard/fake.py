"""Local fakes of the two filters' APIs (plain http on 127.0.0.1). They record every request and can play refusals."""
import base64
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


class FakeAdguard(_Fake):
    def __init__(self, user=USER, password=PASSWORD, data=None):
        self.user, self.password, self.data = user, password, dict(data or fx.DATA)
        super().__init__()

    def handle(self, h, method, body):
        want = "Basic " + base64.b64encode(("%s:%s" % (self.user, self.password)).encode()).decode()
        if h.headers.get("Authorization") != want:
            return h._send(401, {})
        path = urlsplit(h.path).path
        table = {"/control/status": self.data["status"], "/control/stats": self.data["stats"],
                 "/control/filtering/status": self.data["filtering"]}
        if path in table:
            return h._send(200, table[path])
        return h._send(404, {})
