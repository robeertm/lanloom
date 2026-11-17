"""A local fake of the firewall's XML API (plain http on 127.0.0.1): one POST form field ``reqxml`` per request.
Records every request, checks the account from the XML, and can play refusals, HTTP errors and an old firmware."""
import threading
import xml.etree.ElementTree as ET
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import fixtures as fx

USER = "lanloom-read"
PASSWORD = "s3cret"          # test value only


def _split(reply: bytes) -> dict:
    """Fixture reply -> {entity tag: [serialised element, ...]}."""
    out = defaultdict(list)
    for child in ET.fromstring(reply):
        if child.tag not in ("Login", "Status"):
            out[child.tag].append(ET.tostring(child, encoding="unicode"))
    return dict(out)


class FakeFw:
    def __init__(self, user=USER, password=PASSWORD, reply=fx.REPLY):
        self.user, self.password = user, password
        self.entities = _split(reply)
        self.mode = "ok"            # ok | ip_refused | api_off | http | old_firmware
        self.http_status = 500
        self.requests = []          # dicts: method, path, headers, body, reqxml, asked
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, data: bytes):
                self.send_response(code)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer._record(self, "GET", b"")
                self._send(405, b"")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer._send(self, "POST", self.rfile.read(n))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    # ------------------------------------------------------------------
    def _record(self, h, method, body):
        form = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        rec = {"method": method, "path": h.path, "headers": dict(h.headers.items()), "body": body, "form": form,
               "reqxml": (form.get("reqxml") or [None])[0], "asked": [], "user": None, "password": None}
        if rec["reqxml"]:
            try:
                root = ET.fromstring(rec["reqxml"])
                rec["user"] = root.findtext("Login/Username")
                rec["password"] = root.findtext("Login/Password")
                get = root.find("Get")
                rec["asked"] = [e.tag for e in (get if get is not None else [])]
            except ET.ParseError:
                pass
        with self._lock:
            self.requests.append(rec)
        return rec

    def _send(self, h, method, body):
        rec = self._record(h, method, body)
        if self.mode == "http":
            return h._send(self.http_status, b"<html>error</html>")
        if self.mode == "ip_refused":
            return h._send(200, fx.IP_REFUSED)
        if self.mode == "api_off":
            return h._send(200, fx.API_OFF)
        if rec["user"] != self.user or rec["password"] != self.password:
            return h._send(200, fx.wrap("", login=fx.LOGIN_FAIL))
        asked = rec["asked"]
        if self.mode == "old_firmware" and "NATRule" in asked:
            err = '<Status code="500">Unknown entity NATRule</Status>'
            return h._send(200, fx.wrap(err))     # combined or single: root status, no entities
        body = "".join("".join(self.entities.get(tag, [])) for tag in asked)
        return h._send(200, fx.wrap(body))

    def set_entity(self, tag, xml_snippets):
        self.entities[tag] = list(xml_snippets)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)
