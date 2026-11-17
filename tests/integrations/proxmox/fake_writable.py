"""A stateful fake of a two-node host's API for the write half, over plain HTTP on 127.0.0.1 (no real network).

Unlike ``fake_pve`` (fixed, GET only) this one keeps state: per-guest configuration with a digest that changes on
every write, firewall options and rule lists with one digest per guest, power state, tasks (UPID) for start and
shutdown, pending configuration changes, and it refuses a write whose digest does not match with HTTP 500 and
``{"errors": {"digest": ...}}`` like the real host. Bodies are parsed as form-encoded (what the transport sends).

Knobs for tests: ``task_mode`` (ok, slow, failed, never, noeffect), ``pending_slots`` (card changes that stay pending),
``permissions`` (what ``/access/permissions`` tells about the write token), ``fail_next`` (errors on demand),
``on_write`` (a callback that runs once right before the next write is processed: a change made on the host in
between). Every request is recorded in ``requests`` (method, path, query, parsed body, lower-case headers).
Sample values only: addresses from 192.168.50.0/24, locally administered hardware addresses (02:4c:4c:...).
"""
import copy
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import fixtures as fx

TOKEN_ID = "lanloom@pve!write"
SECRET = "7f3a9c2e-5b1d-4e68-9a07-c4d8e1b2f356"       # secret-scan: fake
API = "/api2/json"
NODES = ("node-a", "node-b")
PERMISSIONS_WRITE = {"/": {"Sys.Audit": 1}, "/pool/lab": {"VM.Audit": 1, "VM.PowerMgmt": 1, "VM.Config.Network": 1, "SDN.Use": 1}}
PERMISSIONS_AUDIT = {"/": {"Sys.Audit": 1, "VM.Audit": 1}}
PERMISSIONS_BROAD = {"/": {"Sys.Audit": 1, "Sys.Modify": 1, "VM.PowerMgmt": 1, "VM.Config.Network": 1}}

# key -> (node, name, status, extras for the resource row, config)
GUESTS = {
    "qemu/101": ("node-a", "app", "running", {}, {
        "name": "app", "cores": 2, "memory": "4096", "agent": "0",
        "net0": "virtio=02:4C:4C:00:0A:01,bridge=vmbr0,firewall=1,tag=20",
        "net1": "e1000=02:4C:4C:00:0A:11,bridge=vmbr1"}),
    "qemu/102": ("node-a", "batch", "stopped", {}, {
        "name": "batch", "cores": 1, "memory": "2048", "net0": "virtio=02:4C:4C:00:0B:01,bridge=vmbr0,tag=30"}),
    "lxc/103": ("node-a", "executor", "running", {}, {
        "hostname": "executor", "cores": 1, "memory": 512,
        "net0": "name=eth0,bridge=vmbr0,hwaddr=02:4C:4C:00:0C:01,ip=192.168.50.13/24,gw=192.168.50.1,type=veth"}),
    "qemu/104": ("node-a", "tpl", "stopped", {"template": 1}, {
        "name": "tpl", "template": 1, "net0": "virtio=02:4C:4C:00:0D:01,bridge=vmbr0"}),
    "qemu/105": ("node-a", "locked", "running", {"lock": "backup"}, {
        "name": "locked", "lock": "backup", "net0": "virtio=02:4C:4C:00:0E:01,bridge=vmbr0"}),
    "qemu/106": ("node-a", "ha-guest", "running", {"hastate": "started"}, {
        "name": "ha-guest", "net0": "virtio=02:4C:4C:00:0F:01,bridge=vmbr0"}),
    "qemu/107": ("node-a", "marked", "running", {"tags": "lanloom-protect;infra"}, {
        "name": "marked", "net0": "virtio=02:4C:4C:00:10:01,bridge=vmbr0"}),
    "qemu/108": ("node-a", "plain", "running", {}, {
        "name": "plain", "net0": "virtio=02:4C:4C:00:11:01,bridge=vmbr0,tag=20"}),
    "lxc/201": ("node-b", "far", "running", {}, {
        "hostname": "far", "net0": "name=eth0,bridge=vmbr0,hwaddr=02:4C:4C:00:12:01,ip=dhcp,type=veth"}),
}
RULES = {
    "qemu/101": [
        {"type": "in", "action": "ACCEPT", "enable": 1, "proto": "tcp", "dport": "443", "comment": "https"},
        {"type": "in", "action": "DROP", "enable": 1, "proto": "tcp", "dport": "8080", "source": "192.168.50.0/24",
         "comment": "lanloom: test"},
        {"type": "in", "action": "ACCEPT", "enable": 1, "macro": "SSH"},
    ],
}
FW_ENABLE = {"qemu/101": 1, "qemu/108": 0}


def _sha(obj, ver) -> str:
    return hashlib.sha1(("%d:%s" % (ver, json.dumps(obj, sort_keys=True))).encode()).hexdigest()


class FakeWritable:
    def __init__(self, permissions=None):
        self.permissions = copy.deepcopy(PERMISSIONS_WRITE if permissions is None else permissions)
        self.requests = []             # {"method", "path", "query", "body", "headers"}
        self.task_mode = "ok"          # ok | slow | failed | never | noeffect
        self.task_polls = 2            # polls a "slow" task needs before it stops
        self.pending_slots = set()     # card slots whose change stays pending (guest needs a restart)
        self.fail = []                 # [method, path regex, status, remaining]
        self.on_write = None           # callable run once right before the next write is processed
        self.tasks = {}
        self.res = {}
        self.cfg, self.pend, self.cfg_ver = {}, {}, {}
        self.rules, self.fw_opts, self.fw_ver = {}, {}, {}
        for key, (node, name, status, extra, cfg) in GUESTS.items():
            kind, vmid = key.split("/")
            self.res[key] = dict({"id": key, "type": kind, "vmid": int(vmid), "node": node, "name": name, "status": status,
                                  "template": 0}, **extra)
            self.cfg[key], self.pend[key], self.cfg_ver[key] = dict(cfg), {}, 1
            self.rules[key] = copy.deepcopy(RULES.get(key, []))
            self.fw_opts[key] = {"enable": FW_ENABLE.get(key, 0), "policy_in": "DROP", "policy_out": "ACCEPT"}
            self.fw_ver[key] = 1
        self._n = 0
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    # ------------------------------------------------------------------ views for tests
    @property
    def writes(self):
        return [r for r in self.requests if r["method"] != "GET"]

    def methods(self):
        return {r["method"] for r in self.requests}

    def count(self, rx, method=None):
        return sum(1 for r in self.requests if re.search(rx, r["path"]) and (method is None or r["method"] == method))

    def net(self, key, slot="net0"):
        return self.cfg[key].get(slot)

    def status(self, key):
        return self.res[key]["status"]

    def rule_list(self, key):
        return self.rules[key]

    def digest(self, key):
        return _sha({"c": self.cfg[key], "p": self.pend[key]}, self.cfg_ver[key])

    def fw_digest(self, key):
        return _sha({"o": self.fw_opts[key], "r": self.rules[key]}, self.fw_ver[key])

    def external_config_change(self, key, **fields):
        """A change made on the host by someone else (the digest moves on)."""
        self.cfg[key].update(fields)
        self.cfg_ver[key] += 1

    def external_rule_change(self, key):
        self.fw_ver[key] += 1

    def fail_next(self, method, rx, status=500, times=1):
        self.fail.append([method, rx, status, times])

    # ------------------------------------------------------------------ reads
    def _rule_items(self, key):
        d = self.fw_digest(key)
        return [dict(r, pos=i, digest=d) for i, r in enumerate(self.rules[key])]

    def _tasks_status(self, node, upid):
        t = self.tasks.get(upid)
        if t is None or t["node"] != node:
            return 404, None
        if t["mode"] in ("ok", "noeffect"):
            return 200, {"status": "stopped", "exitstatus": "OK", "upid": upid}
        if t["mode"] == "failed":
            return 200, {"status": "stopped", "exitstatus": "command 'qm %s %s' failed: exit code 255" % (t["op"], t["vmid"]), "upid": upid}
        if t["mode"] == "slow":
            t["polls"] += 1
            if t["polls"] > t["need"]:
                if not t["applied"]:
                    t["applied"] = True
                    self.res[t["key"]]["status"] = t["effect"]
                return 200, {"status": "stopped", "exitstatus": "OK", "upid": upid}
        return 200, {"status": "running", "upid": upid}

    def reply(self, method, path, query, body):
        rel = path[len(API):] if path.startswith(API) else None
        if rel is None:
            return 404, None
        if method == "GET":
            fixed = {"/version": fx.VERSION, "/cluster/status": fx.CLUSTER, "/access/permissions": self.permissions,
                     "/cluster/sdn/zones": fx.SDN["zones"], "/cluster/sdn/vnets": fx.SDN["vnets"],
                     "/cluster/resources": [dict(r) for r in fx.RESOURCES if r["type"] == "node"] + [dict(r) for r in self.res.values()]}
            if rel in fixed:
                return 200, copy.deepcopy(fixed[rel])
            m = re.fullmatch(r"/nodes/([\w.-]+)/network", rel)
            if m:
                return (200, fx.NODE_NETWORK[m.group(1)]) if m.group(1) in fx.NODE_NETWORK else (404, None)
            m = re.fullmatch(r"/nodes/([\w.-]+)/tasks/(.+)/status", rel)
            if m:
                return self._tasks_status(m.group(1), m.group(2))
        m = re.fullmatch(r"/nodes/([\w.-]+)/(qemu|lxc)/(\d+)/(.+)", rel)
        if not m:
            return 404, None
        node, kind, vmid, what = m.groups()
        key = "%s/%s" % (kind, vmid)
        if key not in self.res or self.res[key]["node"] != node:
            return 404, None
        return self._guest(method, key, node, what, query, body)

    def _guest(self, method, key, node, what, query, body):
        r = self.res[key]
        if method == "GET":
            if what == "config":
                return 200, dict(self.cfg[key], digest=self.digest(key))
            if what == "pending":
                cur = [{"key": k, "value": v} for k, v in self.cfg[key].items()]
                for item in cur:
                    if item["key"] in self.pend[key]:
                        item["pending"] = self.pend[key][item["key"]]
                return 200, cur
            if what == "status/current":
                out = {"status": r["status"], "vmid": r["vmid"], "name": r["name"]}
                if r.get("lock"):
                    out["lock"] = r["lock"]
                if r.get("hastate"):
                    out["ha"] = {"managed": 1, "state": r["hastate"]}
                return 200, out
            if what == "firewall/options":
                return 200, dict(self.fw_opts[key], digest=self.fw_digest(key))
            if what == "firewall/rules":
                return 200, self._rule_items(key)
            return 404, None
        # ---- writes
        if what == "config" and method == "PUT":
            return self._put_config(key, body)
        if what == "status/start" and method == "POST":
            return self._power(key, node, "start", "running")
        if what == "status/shutdown" and method == "POST":
            if str(body.get("forceStop", "0")) != "0":
                self.res[key]["status"] = "stopped"                  # a hard stop: the real host would do it
            return self._power(key, node, "shutdown", "stopped")
        if what == "firewall/options" and method == "PUT":
            bad = self._check_digest(self.fw_digest(key), body.get("digest"))
            if bad:
                return bad
            if "enable" in body:
                self.fw_opts[key]["enable"] = int(body["enable"])
            self.fw_ver[key] += 1
            return 200, None
        m = re.fullmatch(r"firewall/rules(?:/(\d+))?", what)
        if m:
            return self._rules_write(method, key, m.group(1), query, body)
        return 404, None

    @staticmethod
    def _check_digest(have, sent):
        if sent is not None and sent != have:
            return 500, {"errors": {"digest": "detected modified configuration - file changed by other user! Try again."}}
        return None

    def _put_config(self, key, body):
        bad = self._check_digest(self.digest(key), body.get("digest"))
        if bad:
            return bad
        if "revert" in body:
            for slot in body["revert"].split(","):
                self.pend[key].pop(slot, None)
        for k, v in body.items():
            if k in ("digest", "revert"):
                continue
            if not re.fullmatch(r"net\d+", k):
                return 400, {"errors": {k: "property is not defined"}}
            if k in self.pending_slots:
                self.pend[key][k] = v
            else:
                self.cfg[key][k] = v
        self.cfg_ver[key] += 1
        return 200, None

    def _power(self, key, node, op, effect):
        if self.res[key]["status"] == effect:
            return 500, {"errors": {"status": "VM is already %s" % effect}}
        self._n += 1
        upid = "UPID:%s:0000ABCD:%08X:6553F00D:qm%s:%s:%s:" % (node, 0x1234 + self._n - 1, op, key.split("/")[1], TOKEN_ID)
        mode = self.task_mode
        self.tasks[upid] = {"node": node, "mode": mode, "key": key, "op": op, "vmid": key.split("/")[1], "effect": effect,
                            "polls": 0, "need": self.task_polls, "applied": False}
        if mode in ("ok",):
            self.res[key]["status"] = effect
            self.tasks[upid]["applied"] = True
        return 200, upid

    def _rules_write(self, method, key, pos, query, body):
        rules = self.rules[key]
        digest = body.get("digest") or (query.get("digest") or [None])[0]
        bad = self._check_digest(self.fw_digest(key), digest)
        if bad:
            return bad
        if method == "POST":
            rule = {k: v for k, v in body.items() if k not in ("pos", "digest")}
            at = int(body.get("pos", len(rules)))
            rules.insert(min(at, len(rules)), self._typed(rule))
        else:
            i = int(pos)
            if i >= len(rules):
                return 500, {"errors": {"pos": "no such rule"}}
            if method == "DELETE":
                rules.pop(i)
            else:
                rule = rules[i]
                for k, v in body.items():
                    if k in ("digest", "delete"):
                        continue
                    rule[k] = v
                for k in (body.get("delete") or "").split(","):
                    if k:
                        rule.pop(k, None)
                rules[i] = self._typed(rule)
        self.fw_ver[key] += 1
        return 200, None

    @staticmethod
    def _typed(rule):
        if "enable" in rule:
            rule["enable"] = int(rule["enable"])
        return rule

    # ------------------------------------------------------------------ server
    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, data=None):
                if status == 200:
                    body = json.dumps({"data": data}).encode()
                elif isinstance(data, dict):
                    body = json.dumps(dict(data, data=None)).encode()
                else:
                    body = b"{}"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8") if n else ""
                body = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
                parts = urlsplit(self.path)
                path = unquote(parts.path)
                with fake._lock:
                    fake.requests.append({"method": self.command, "path": path, "query": parts.query, "body": body, "raw": raw,
                                          "headers": {k.lower(): v for k, v in self.headers.items()}})
                    if self.headers.get("Authorization") != "PVEAPIToken=%s=%s" % (TOKEN_ID, SECRET):
                        return self._send(401)
                    if self.command != "GET" and fake.on_write:
                        hook, fake.on_write = fake.on_write, None
                        hook()
                    for f in fake.fail:
                        if f[3] > 0 and f[0] == self.command and re.search(f[1], path):
                            f[3] -= 1
                            return self._send(f[2])
                    status, data = fake.reply(self.command, path, parse_qs(parts.query), body)
                return self._send(status, data)

            do_GET = do_POST = do_PUT = do_DELETE = _any

        return H
