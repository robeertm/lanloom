"""A stateful fake of the router's ubus JSON-RPC endpoint (rpcd behind uhttpd) over plain HTTP on 127.0.0.1.

Built after the rpcd sources (``session.c``, ``uci.c``) and uhttpd's ubus handler:

* ``session login`` with the all-zero session returns a session id and the login's access list; every other call
  needs a live session whose access list names the object and method, else the JSON-RPC error -32002 ("access
  denied") - the same answer uhttpd gives for an expired session;
* ``uci`` changes are staged per session (``uci get`` of that session shows them, other sessions do not) until
  ``uci apply``; ``changes`` lists and ``revert`` drops them; a change of a configuration the access list does not
  allow to write answers 6 (permission denied);
* ``uci apply`` with ``rollback`` keeps a copy of the configuration from before, and unless ``uci confirm`` comes
  before the timeout, the copy comes back (``expire()`` plays "the timeout passed"); ``uci rollback`` brings it back at
  once; a second apply while one waits answers 6. Like the real rpcd (checked against a 23.05 rootfs), confirm and
  rollback are accepted from any session whose access list allows them, not only from the one that applied;
* like UCI, an anonymous section is named when the file is loaded, from its position and type (``cfgXXYYYY``), so its
  name changes when sections before it are added or removed (a changed value keeps the name, as on a real router).

Knobs: ``fail_next`` ({(object, method): code}), ``on_write`` (callback once before the next change), ``lose_answer``
(the next ``uci add`` is stored but the connection drops before the answer), ``drop_after_apply`` (every request after
the next apply fails, as when a plan cut the executor off), ``expire_sessions()``, ``no_rollback`` (an old rpcd that
ignores the rollback argument). Every request is recorded (object, method, arguments without passwords).
"""
import copy
import hashlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import fixtures as fx

ZERO = "0" * 32
WRITES = ("add", "set", "delete", "changes", "revert", "apply", "confirm", "rollback", "rename", "order")


def _type_hash(s: dict) -> int:
    return int(hashlib.sha256(s[".type"].encode()).hexdigest(), 16) % 65536


def view(stored: list) -> list:
    """Sections as UCI hands them out after loading: names for anonymous ones, ``.index`` in file order."""
    out = []
    for i, s in enumerate(stored):
        d = copy.deepcopy(s)
        if d.get(".anonymous"):
            d[".name"] = "cfg%02x%04x" % (i + 1, _type_hash(s))
        d[".index"] = i
        out.append(d)
    return out


def strip(sections: list) -> list:
    out = []
    for s in sections:
        d = {k: v for k, v in s.items() if k != ".index"}
        if d.get(".anonymous"):
            d.pop(".name", None)
        out.append(d)
    return out


class FakeUbus:
    def __init__(self, version="23.05", wireless=True, helper=True):
        self.version = version
        self.stored = {k: copy.deepcopy(f(version)) for k, f in fx.CONFIGS.items()}
        if not wireless:
            del self.stored["wireless"]
        self.helper = helper
        self.wireless = wireless
        self.logins = {fx.READ_USER: (fx.READ_PASSWORD, fx.acls(False)), fx.WRITE_USER: (fx.WRITE_PASSWORD, fx.acls(True))}
        self.sessions = {}                  # sid -> {"user", "acls", "ops": {pkg: [staged changes]}}
        self.pending = None                 # {"sid", "copy", "timeout"} while an apply with rollback waits
        self.applies = []                   # (rollback, timeout) of every apply
        self.confirms = 0
        self.rollbacks = 0                  # immediate rollbacks asked by a client
        self.expired = 0                    # the timer ran out
        self.fail_next = {}
        self.on_write = None
        self.lose_answer = False
        self.drop_after_apply = False
        self.no_rollback = False
        self._dropped = False
        self.requests = []
        self.login_count = 0
        self._lock = threading.RLock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer._handle(self, self.rfile.read(n))

            def do_GET(self):
                self.send_response(404)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------------ helpers for tests
    def committed(self, pkg="firewall") -> dict:
        return {s[".name"]: s for s in view(self.stored[pkg])}

    def rule(self, name_option: str, pkg="firewall"):
        return next((s for s in view(self.stored[pkg]) if s[".type"] == "rule" and s.get("name") == name_option), None)

    def calls(self, method=None):
        return [(o, m, a) for o, m, a in self.requests if method is None or m == method]

    def changes_made(self):
        return [m for o, m, a in self.requests if o == "uci" and m in ("add", "set", "delete", "apply", "confirm", "rollback", "revert")]

    def expire(self):
        """The rollback timeout passed without a confirmation: the copy from before comes back."""
        with self._lock:
            if self.pending:
                self.stored = copy.deepcopy(self.pending["copy"])
                self.pending = None
                self.expired += 1

    def expire_sessions(self):
        with self._lock:
            self.sessions = {}

    def restore_link(self):
        self.drop_after_apply = False
        self._dropped = False

    def edit(self, name_option, **opts):
        """Someone changes a rule on the router directly (committed at once)."""
        for s in self.stored["firewall"]:
            if s.get(".type") == "rule" and s.get("name") == name_option:
                s.update(opts)

    # ------------------------------------------------------------------ HTTP
    def _send(self, h, obj):
        data = json.dumps(obj).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def _drop(self, h):
        h.close_connection = True
        try:
            h.connection.shutdown(2)
        except OSError:
            pass

    def _handle(self, h, body):
        with self._lock:
            if self._dropped:
                return self._drop(h)
            try:
                doc = json.loads(body.decode())
                n, (sid, obj, method, args) = doc["id"], doc["params"]
                assert doc["jsonrpc"] == "2.0" and doc["method"] == "call"
            except Exception:
                return self._send(h, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            self.requests.append((obj, method, {k: v for k, v in args.items() if k != "password"}))
            if (obj, method) == ("session", "login"):
                return self._send(h, {"jsonrpc": "2.0", "id": n, "result": self._login(sid, args)})
            sess = self.sessions.get(sid)
            if sess is None or method not in (sess["acls"]["ubus"].get(obj) or []):
                return self._send(h, {"jsonrpc": "2.0", "id": n, "error": {"code": -32002, "message": "Access denied"}})
            code = self.fail_next.pop((obj, method), None)
            if code is not None:
                return self._send(h, {"jsonrpc": "2.0", "id": n, "result": [code]})
            if obj == "uci" and method in ("add", "set", "delete") and self.on_write:
                cb, self.on_write = self.on_write, None
                cb(self)
            res = self._call(sid, sess, obj, method, args)
            if obj == "uci" and method == "add" and self.lose_answer:
                self.lose_answer = False
                return self._drop(h)
            if obj == "uci" and method == "apply" and res == [0] and self.drop_after_apply:
                self._dropped = True
            return self._send(h, {"jsonrpc": "2.0", "id": n, "result": res})

    def _login(self, sid, args):
        if sid != ZERO:
            return [2]
        got = self.logins.get(args.get("username"))
        if not got or got[0] != args.get("password"):
            return [6]
        self.login_count += 1
        new = secrets.token_hex(16)
        self.sessions[new] = {"user": args["username"], "acls": copy.deepcopy(got[1]), "ops": {}}
        return [0, {"ubus_rpc_session": new, "timeout": int(args.get("timeout") or 300), "expires": 300,
                    "acls": copy.deepcopy(got[1]), "data": {"username": args["username"]}}]

    # ------------------------------------------------------------------ objects
    def _call(self, sid, sess, obj, method, args):
        v = self.version
        if obj == "system":
            return [0, fx.board(v)] if method == "board" else [0, fx.info()]
        if obj == "network.interface":
            return [0, fx.iface_dump(v)]
        if obj == "network.device":
            return [0, fx.device_status(v)]
        if obj == "luci-rpc":
            if not self.helper:
                return [3]
            return [0, fx.hints()] if method == "getHostHints" else [0, fx.leases()]
        if obj == "iwinfo":
            if not self.wireless:
                return [0, {"devices": []}] if method == "devices" else [4]
            if method == "devices":
                return [0, {"devices": list(fx.AIR)}]
            dev = args.get("device")
            if dev not in fx.AIR:
                return [4]
            return [0, fx.iwinfo_info(dev)] if method == "info" else [0, fx.iwinfo_assoc(dev)]
        if obj == "uci":
            return self._uci(sid, sess, method, args)
        return [3]

    def _view(self, sess, pkg) -> list:
        """The session's view: the stored file as UCI loads it now, with the session's staged changes replayed on
        top (like rpcd's per-session delta files); a change whose section is gone is skipped."""
        secs = view(self.stored[pkg])
        for op in sess["ops"].get(pkg) or []:
            if op[0] == "add":
                if not any(x[".name"] == op[1] for x in secs):
                    d = {".type": op[2], ".anonymous": False, ".name": op[1], ".index": len(secs)}
                    d.update(copy.deepcopy(op[3]))
                    secs.append(d)
                continue
            x = next((x for x in secs if x[".name"] == op[1]), None)
            if x is None:
                continue
            if op[0] == "set":
                x.update(copy.deepcopy(op[2]))
            elif op[2] is None:
                secs.remove(x)
            else:
                x.pop(op[2], None)
        return secs

    def _may_write(self, sess, pkg) -> bool:
        return "write" in (sess["acls"]["uci"].get(pkg) or [])

    def _uci(self, sid, sess, method, args):
        pkg = args.get("config")
        if method in ("get", "add", "set", "delete", "changes", "revert") and pkg not in self.stored:
            return [4]
        if method == "get":
            if "read" not in (sess["acls"]["uci"].get(pkg) or []):
                return [6]
            return [0, {"values": {x[".name"]: copy.deepcopy(x) for x in self._view(sess, pkg)}}]
        if method in ("add", "set", "delete", "revert") and not self._may_write(sess, pkg):
            return [6]
        ops = sess["ops"].setdefault(pkg, [])
        if method == "add":
            name = args.get("name")
            if not name or any(x[".name"] == name for x in self._view(sess, pkg)):
                return [2]
            ops.append(("add", name, args["type"], copy.deepcopy(args.get("values") or {})))
            return [0, {"section": name}]
        if method == "set":
            x = next((x for x in self._view(sess, pkg) if x[".name"] == args.get("section")), None)
            if x is None:
                return [4]
            ops.append(("set", x[".name"], copy.deepcopy(args.get("values") or {})))
            return [0]
        if method == "delete":
            x = next((x for x in self._view(sess, pkg) if x[".name"] == args.get("section")), None)
            if x is None or ("option" in args and args["option"] not in x):
                return [4]
            ops.append(("delete", x[".name"], args.get("option")))
            return [0]
        if method == "changes":
            return [0, {"changes": [list(op[:3]) for op in sess["ops"].get(pkg) or []]}]
        if method == "revert":
            sess["ops"].pop(pkg, None)
            return [0]
        if method == "apply":
            if self.pending:
                return [6]
            rollback = bool(args.get("rollback")) and not self.no_rollback
            self.applies.append((rollback, args.get("timeout")))
            if rollback:
                self.pending = {"sid": sid, "copy": copy.deepcopy(self.stored), "timeout": args.get("timeout") or 30}
            for p in [p for p, o in sess["ops"].items() if o]:
                if not self._may_write(sess, p):
                    return [6]
                self.stored[p] = strip(self._view(sess, p))
            sess["ops"] = {}
            return [0]
        if method == "confirm":
            if not self.pending:
                return [5]
            self.pending = None
            self.confirms += 1
            return [0]
        if method == "rollback":
            if not self.pending:
                return [5]
            self.stored = copy.deepcopy(self.pending["copy"])
            self.pending = None
            self.rollbacks += 1
            return [0]
        return [3]
