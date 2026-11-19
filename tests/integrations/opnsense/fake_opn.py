"""A stateful fake of the firewall's REST API over plain HTTP on 127.0.0.1 (no real network).

GET answers come from ``fixtures.replies(version)``; the rules of the rule API are live state: ``.../filter/get``,
``getRule/<uuid>`` and ``searchRule`` reflect every ``addRule``/``setRule``/``delRule``. Like the real API it saves a
change into the configuration (``config``) and puts it live only on ``apply`` (``live``). Releases with the rule
API's own way back (``savepoint``) keep the savepoint, ``apply/<revision>`` arms the 60 second timer, and
``cancelRollback/<revision>`` stops it; ``expire()`` plays "60 seconds passed without a confirmation" (the rules of the
savepoint come back, as the firewall's timer script does). Releases from 26.7 answer the savepoint paths with 404.

Credentials: Basic authentication; ``keys`` maps key -> (secret, may_write). A read key that posts is answered 403
(the firewall itself would accept it if the user had the page - the fake is stricter on purpose so tests notice).
Knobs: ``fail_next`` ({path fragment: status}), ``on_write`` (callback once before the next write), ``lose_answer``
(the next addRule stores the rule but the answer is cut off), ``drop_after_apply`` (reads fail after the next apply,
as when a plan cut the executor off). Every request is recorded (method, path, body, auth key only).
"""
import base64
import copy
import json
import re
import threading
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import fixtures as fx

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
REV = r"\d{9,11}(?:\.\d{1,6})?"


class FakeOpn:
    def __init__(self, version="24.7"):
        self.version = version
        self.replies = fx.replies(version)
        self.net_shape = "plain" if version >= "26.1" else "option"
        self.config = fx.rules(self.net_shape)          # uuid -> rule in the .../get shape
        self.live = copy.deepcopy(self.config)
        self.savepoints = {}                             # revision -> copy of the rules
        self.armed = None                                # revision whose timer runs
        self.applies = []
        self.keys = {fx.API_KEY: (fx.API_SECRET, False), fx.WRITE_KEY: (fx.WRITE_SECRET, True)}
        self.fail_next = {}
        self.on_write = None
        self.lose_answer = False
        self.drop_after_apply = False
        self._dropped = False
        self.requests = []
        self._rev = 1763500000
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

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------------ helpers
    @property
    def native(self) -> bool:
        return self.version < "26.7"

    def expire(self):
        """60 seconds passed without cancelRollback: the firewall puts the savepoint's rules back and reloads."""
        with self._lock:
            if self.armed and self.armed in self.savepoints:
                self.config = copy.deepcopy(self.savepoints[self.armed])
                self.live = copy.deepcopy(self.config)
            self.armed = None

    def writes(self):
        return [(r["method"], re.sub(r"/%s\Z" % REV, "/<rev>", re.sub(UUID, "<uuid>", r["path"]))) for r in self.requests
                if r["method"] == "POST"]

    def _who(self, h):
        auth = h.headers.get("Authorization") or ""
        if not auth.startswith("Basic "):
            return None, False
        try:
            key, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
        except Exception:
            return None, False
        want = self.keys.get(key)
        if not want or want[0] != secret:
            return None, False
        return key, want[1]

    def _send(self, h, code, obj):
        data = json.dumps(obj).encode() if obj is not None else b""
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def _search_rows(self):
        out = []
        for u, r in self.config.items():
            out.append({"uuid": u, "enabled": r["enabled"], "sequence": r["sequence"], "description": r["description"],
                        "interface": ",".join(fx_values(r["interface"]))})
        if self.version >= "26.1":
            out += fx.legacy_rows()
        return fx.search(out)

    # ------------------------------------------------------------------ requests
    def _handle(self, h, method, body):
        path = h.path
        key, may_write = self._who(h)
        rec = {"method": method, "path": path, "key": key, "body": None}
        if body:
            try:
                rec["body"] = json.loads(body.decode())
            except ValueError:
                rec["body"] = "<not json>"
            rec["content_type"] = h.headers.get("Content-Type")
        with self._lock:
            self.requests.append(rec)
        if key is None:
            return self._send(h, 401, {"status": 401, "message": "Authentication Failed"})
        for frag, status in list(self.fail_next.items()):
            if frag in path:
                del self.fail_next[frag]
                return self._send(h, status, {"errorMessage": "fail on demand"})
        if self._dropped and method == "GET":
            return self._send(h, 503, {"errorMessage": "gone"})
        if method == "GET":
            return self._get(h, path)
        if not may_write:
            return self._send(h, 403, {"status": 403, "message": "Forbidden"})
        if self.on_write:
            cb, self.on_write = self.on_write, None
            cb(self)
        return self._post(h, path, rec["body"])

    def _get(self, h, path):
        with self._lock:
            if path == "/api/firewall/filter/get":
                obj = fx.filter_get(self.net_shape)
                obj["filter"]["rules"]["rule"] = copy.deepcopy(self.config)
                return self._send(h, 200, obj)
            m = re.fullmatch(r"/api/firewall/filter/getRule/(%s)" % UUID, path)
            if m:
                r = self.config.get(m.group(1))
                return self._send(h, 200, {"rule": copy.deepcopy(r)} if r else [])
            if path == "/api/firewall/filter/searchRule":
                return self._send(h, 200, self._search_rows())
            if path in self.replies:
                return self._send(h, 200, self.replies[path])
        return self._send(h, 404, {"errorMessage": "Endpoint not found"})

    def _validate(self, rule, partial=False):
        errs = {}
        if not partial or "description" in rule:
            if len(str(rule.get("description", ""))) > 255:
                errs["rule.description"] = "too long"
        if "interface" in rule and rule["interface"] not in [i[0] for i in fx.IFACES] + ["homenets"]:
            errs["rule.interface"] = "unknown"
        if "action" in rule and rule["action"] not in ("pass", "block", "reject"):
            errs["rule.action"] = "invalid"
        if "destination_port" in rule and rule["destination_port"] and not re.fullmatch(r"\d+(:\d+)?", rule["destination_port"]):
            errs["rule.destination_port"] = "invalid"
        return errs

    def _store(self, u, plain, base=None):
        r = copy.deepcopy(base) if base else fx.rule("", "")
        for k, v in plain.items():
            if k in ("action", "direction", "ipprotocol", "protocol", "statetype"):
                r[k] = {o: {"value": o, "selected": 1 if o == v else 0} for o in (set(r[k]) | {v})}
            elif k == "interface":
                r[k] = fx.iface_opt(v.split(",") if v else [])
            elif k in ("source_net", "destination_net"):
                r[k] = v if self.net_shape == "plain" else {x: {"value": x, "selected": 1} for x in v.split(",")}
            else:
                r[k] = v
        self.config[u] = r

    def _post(self, h, path, body):
        if not isinstance(body, dict):
            return self._send(h, 400, {"errorMessage": "Invalid JSON syntax"})
        with self._lock:
            if path == "/api/firewall/filter/addRule":
                rule = body.get("rule") or {}
                errs = self._validate(rule)
                if errs:
                    return self._send(h, 200, {"result": "failed", "validations": errs})
                u = str(uuidlib.uuid4())
                self._store(u, rule)
                if self.lose_answer:
                    self.lose_answer = False
                    h.close_connection = True
                    return None                              # stored, but no answer
                return self._send(h, 200, {"result": "saved", "uuid": u})
            m = re.fullmatch(r"/api/firewall/filter/setRule/(%s)" % UUID, path)
            if m:
                if m.group(1) not in self.config:
                    return self._send(h, 200, {"result": "failed"})
                rule = body.get("rule") or {}
                errs = self._validate(rule, partial=True)
                if errs:
                    return self._send(h, 200, {"result": "failed", "validations": errs})
                self._store(m.group(1), rule, self.config[m.group(1)])
                return self._send(h, 200, {"result": "saved"})
            m = re.fullmatch(r"/api/firewall/filter/delRule/(%s)" % UUID, path)
            if m:
                gone = self.config.pop(m.group(1), None)
                return self._send(h, 200, {"result": "deleted" if gone else "not found"})
            if path == "/api/firewall/filter/savepoint":
                if not self.native:
                    return self._send(h, 404, {"errorMessage": "Endpoint not found"})
                self._rev += 7
                rev = "%d.%04d" % (self._rev, 1234)
                self.savepoints[rev] = copy.deepcopy(self.config)
                return self._send(h, 200, {"status": "ok", "retention": "60", "revision": rev})
            m = re.fullmatch(r"/api/firewall/filter/apply(?:/(%s))?" % REV, path)
            if m:
                if m.group(1) and not self.native:
                    return self._send(h, 404, {"errorMessage": "Endpoint not found"})
                if m.group(1):
                    self.armed = m.group(1)
                self.live = copy.deepcopy(self.config)
                self.applies.append(m.group(1))
                if self.drop_after_apply:
                    self.drop_after_apply = False
                    self._dropped = True
                return self._send(h, 200, {"status": "OK\n\n"})
            m = re.fullmatch(r"/api/firewall/filter/cancelRollback/(%s)" % REV, path)
            if m:
                if not self.native:
                    return self._send(h, 404, {"errorMessage": "Endpoint not found"})
                if self.armed == m.group(1):
                    self.armed = None
                return self._send(h, 200, {"status": "OK"})
        return self._send(h, 404, {"errorMessage": "Endpoint not found"})

    def restore_link(self):
        self._dropped = False


def fx_values(v):
    if isinstance(v, dict):
        return [k for k, o in v.items() if isinstance(o, dict) and o.get("selected")]
    return [x for x in str(v or "").split(",") if x]
