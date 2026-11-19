"""A local stand-in for the router's REST interface (plain http on 127.0.0.1). Checks Basic authentication on every
request, answers the GET paths the read half knows, answers every other method with 405 *and records it*, and can play:
a wrong password, a user that may not read a path (403), a release that lacks a path (404 or 400), HTTP errors, a
reply that is not JSON. Never records the password."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lanloom.integrations.mikrotik import read

from . import fixtures as fx

PATH_KEY = {path: key for key, path in dict(read.LIGHT, **read.FULL).items()}
ACCOUNT = {path: key for key, path in read.ACCOUNT_PATHS.items()}


class FakeRouter:
    def __init__(self, data=None, group="viewonly"):
        self.data = data if data is not None else fx.raw_copy()
        self.users = fx.users(group)
        self.groups = fx.GROUPS
        self.mode = "ok"              # ok | http | html
        self.http_status = 500
        self.denied = set()           # path fragments answered with 403
        self.missing = set()          # path fragments answered with 404
        self.bad_request = set()      # path fragments answered with 400 (a package that is not installed)
        self.requests = []            # dicts: method, path, body, has_auth, headers (without Authorization)
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer._handle(self, "GET")

            def _other(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                outer._handle(self, method, self.rfile.read(n) if n else b"")

            def do_POST(self):
                self._other("POST")

            def do_PUT(self):
                self._other("PUT")

            def do_PATCH(self):
                self._other("PATCH")

            def do_DELETE(self):
                self._other("DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def _send(self, h, status, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def _auth_ok(self, h) -> bool:
        auth = h.headers.get("Authorization") or ""
        if not auth.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return user == fx.USER and pw == fx.PASSWORD

    def _handle(self, h, method, body=b""):
        rec = {"method": method, "path": h.path, "body": body, "has_auth": bool(h.headers.get("Authorization")),
               "headers": {k: v for k, v in h.headers.items() if k.lower() != "authorization"}}
        with self._lock:
            self.requests.append(rec)
        if self.mode == "http":
            return self._send(h, self.http_status, b"<html>error</html>")
        if not self._auth_ok(h):
            return self._send(h, 401, {"error": 401, "message": "Unauthorized"})
        if method != "GET":
            return self._send(h, 405, {"error": 405, "message": "Method Not Allowed"})
        path = h.path
        for frag in self.missing:
            if frag in path:
                return self._send(h, 404, {"error": 404, "message": "Not Found"})
        for frag in self.bad_request:
            if frag in path:
                return self._send(h, 400, {"error": 400, "message": "Bad Request", "detail": "no such command or directory"})
        for frag in self.denied:
            if frag in path:
                return self._send(h, 403, {"error": 403, "message": "Forbidden", "detail": "not enough permissions"})
        if self.mode == "html":
            return self._send(h, 200, b"<html>login</html>")
        if path in ACCOUNT:
            return self._send(h, 200, self.users if ACCOUNT[path] == "users" else self.groups)
        key = PATH_KEY.get(path)
        if key is None:
            return self._send(h, 404, {"error": 404, "message": "Not Found"})
        if key not in self.data:
            return self._send(h, 404, {"error": 404, "message": "Not Found"})
        val = self.data[key]
        return self._send(h, 200, [val] if isinstance(val, dict) else val)

    def paths(self, method="GET"):
        return [r["path"] for r in self.requests if r["method"] == method]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)
