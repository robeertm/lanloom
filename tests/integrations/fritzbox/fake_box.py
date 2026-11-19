"""A local fake of the home router's TR-064 interface (plain http on 127.0.0.1): digest authentication on every SOAP
call (challenge, nonce reuse, a stale nonce once on request, wrong password refused), the SOAP actions of the sample
home, the two documents behind the list paths, UPnP faults. Records every request (never the password)."""
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import fixtures as fx


def _md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


class FakeBox:
    def __init__(self):
        self.answers = fx.services()
        self.hosts_xml = fx.hostlist_xml()
        self.mesh_json = fx.mesh_json()
        self.mode = "ok"              # ok | tr064_off | http
        self.faults = {}              # (path, action) -> UPnP error code answered as HTTP 500
        self.requests = []            # dicts: method, path, action, auth (scheme or ""), headers, body
        self.bad_attempts = 0
        self.good_calls = 0
        self.stale_next = False       # the next authenticated call is answered with a fresh challenge (stale nonce)
        self.nonces = set()
        self.last_nc = {}
        self._n = 0
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

    def _send(self, h, status, body, ctype="text/xml; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    def _challenge(self, h, stale=False):
        with self._lock:
            self._n += 1
            nonce = "%032x" % (0xC0FFEE00 + self._n)
            self.nonces.add(nonce)
        value = 'Digest realm="%s", nonce="%s", algorithm=MD5, qop="auth"%s' % (fx.REALM, nonce, ", stale=true" if stale else "")
        self._send(h, 401, b"", extra={"WWW-Authenticate": value})

    def _auth_ok(self, h, method) -> bool:
        head = h.headers.get("Authorization") or ""
        if not head.startswith("Digest "):
            return False
        f = {k.lower(): (q or v) for k, q, v in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', head)}
        if f.get("nonce") not in self.nonces or f.get("realm") != fx.REALM or f.get("qop") != "auth" or f.get("algorithm") != "MD5":
            return False
        nc = int(f.get("nc", "0"), 16)
        if nc <= self.last_nc.get(f["nonce"], 0):
            return False                                       # a replayed counter
        self.last_nc[f["nonce"]] = nc
        ha1 = _md5("%s:%s:%s" % (fx.USER, fx.REALM, fx.PASSWORD))
        ha2 = _md5("%s:%s" % (method, f.get("uri")))
        want = _md5("%s:%s:%s:%s:auth:%s" % (ha1, f["nonce"], f["nc"], f.get("cnonce"), ha2))
        return f.get("username") == fx.USER and f.get("uri") == h.path and f.get("response") == want

    def _handle(self, h, method, raw):
        action = (h.headers.get("SOAPACTION") or "").strip('"').partition("#")[2]
        auth = h.headers.get("Authorization") or ""
        rec = {"method": method, "path": h.path, "action": action, "auth": auth.split(" ", 1)[0], "body": raw,
               "headers": {k: v for k, v in h.headers.items() if k.lower() != "authorization"}}
        with self._lock:
            self.requests.append(rec)
        if self.mode == "http":
            return self._send(h, 500, b"<html>error</html>", "text/html")
        if method == "GET" and (h.path.startswith("/hostlist.lua?sid=") or h.path.startswith("/meshlist.lua?sid=")):
            if h.path.split("sid=")[1] != fx.SID:
                return self._send(h, 403, b"")
            if h.path.startswith("/hostlist.lua"):
                return self._send(h, 200, self.hosts_xml)
            return self._send(h, 200, self.mesh_json, "application/json")
        if method != "POST" or not h.path.startswith("/upnp/control/"):
            return self._send(h, 405, b"")
        if self.mode == "tr064_off":
            return self._send(h, 404, b"")
        if not auth:
            return self._challenge(h)
        if self.stale_next:
            self.stale_next = False
            self.nonces.clear()
            return self._challenge(h, stale=True)
        if not self._auth_ok(h, method):
            self.bad_attempts += 1
            return self._challenge(h)
        service = (h.headers.get("SOAPACTION") or "").strip('"').partition("#")[0]
        key = (h.path, action)
        if key in self.faults:
            return self._send(h, 500, fx.fault(self.faults[key]))
        self.good_calls += 1
        if key == ("/upnp/control/hosts", "X_AVM-DE_GetHostListPath"):
            return self._send(h, 200, fx.envelope(service, action, {"NewX_AVM-DE_HostListPath": "/hostlist.lua?sid=%s" % fx.SID}))
        if key == ("/upnp/control/hosts", "X_AVM-DE_GetMeshListPath"):
            return self._send(h, 200, fx.envelope(service, action, {"NewX_AVM-DE_MeshListPath": "/meshlist.lua?sid=%s" % fx.SID}))
        if key in self.answers:
            return self._send(h, 200, fx.envelope(service, action, self.answers[key]))
        return self._send(h, 500, fx.fault(401, "Invalid Action"))

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)
