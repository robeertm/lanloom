"""A local fake of a two-node host cluster's API over plain HTTP on 127.0.0.1 (no real network).

Serves the replies of ``fixtures`` under ``/api2/json``; checks the token header; records every request; errors on
demand (``fail``: path regex -> status). GET only - anything else is answered 405 and recorded."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import fixtures as fx

TOKEN_ID = "lanloom@pve!read"
SECRET = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"       # secret-scan: fake
API = "/api2/json"


class FakePve:
    def __init__(self, data=None, permissions=None):
        self.permissions = fx.PERMISSIONS_READ if permissions is None else permissions
        self.requests = []             # (method, path with query, lower-case headers)
        self.fail = {}                 # path regex -> status
        self.resources = [dict(r) for r in fx.RESOURCES]
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

    def methods(self):
        return {r[0] for r in self.requests}

    def paths(self):
        return [r[1] for r in self.requests]

    def count(self, rx):
        return sum(1 for r in self.requests if re.search(rx, r[1]))

    # ------------------------------------------------------------------ routing
    def reply(self, path, query):
        rel = path[len(API):] if path.startswith(API) else None
        if rel is None:
            return 404, None
        fixed = {"/version": fx.VERSION, "/cluster/status": fx.CLUSTER, "/cluster/resources": self.resources,
                 "/cluster/backup": fx.BACKUP_JOBS, "/cluster/backup-info/not-backed-up": fx.NOT_BACKED_UP, "/storage": fx.STORAGE_CFG,
                 "/access/permissions": self.permissions, "/cluster/firewall/options": fx.DC_FW["options"],
                 "/cluster/firewall/rules": fx.DC_FW["rules"], "/cluster/firewall/groups": fx.DC_FW["groups"],
                 "/cluster/firewall/aliases": fx.DC_FW["aliases"], "/cluster/firewall/ipset": fx.DC_FW["ipsets"],
                 "/cluster/sdn/zones": fx.SDN["zones"], "/cluster/sdn/vnets": fx.SDN["vnets"]}
        if rel in fixed:
            return 200, fixed[rel]
        m = re.fullmatch(r"/cluster/firewall/groups/([\w-]+)", rel)
        if m:
            return (200, fx.DC_FW["group_rules"][m.group(1)]) if m.group(1) in fx.DC_FW["group_rules"] else (404, None)
        m = re.fullmatch(r"/cluster/firewall/ipset/([\w-]+)", rel)
        if m:
            return (200, fx.DC_FW["ipset_members"][m.group(1)]) if m.group(1) in fx.DC_FW["ipset_members"] else (404, None)
        m = re.fullmatch(r"/cluster/sdn/vnets/([\w-]+)/subnets", rel)
        if m:
            return 200, fx.SDN["subnets"].get(m.group(1), [])
        m = re.fullmatch(r"/nodes/([\w.-]+)/(status|network|storage|firewall/options|firewall/rules|tasks)", rel)
        if m:
            node, what = m.groups()
            src = {"status": fx.NODE_STATUS, "network": fx.NODE_NETWORK, "storage": fx.NODE_STORAGE, "tasks": fx.TASKS}.get(what)
            if what.startswith("firewall/"):
                return 200, fx.NODE_FW[node]["options" if what.endswith("options") else "rules"]
            if what == "tasks" and query.get("typefilter") != ["vzdump"]:
                return 400, None
            return (200, src[node]) if node in src else (404, None)
        m = re.fullmatch(r"/nodes/([\w.-]+)/storage/([\w.-]+)/content", rel)
        if m:
            return 200, fx.BACKUP_CONTENT.get((m.group(1), m.group(2)), [])
        m = re.fullmatch(r"/nodes/([\w.-]+)/(qemu|lxc)/(\d+)/(.+)", rel)
        if m:
            node, kind, vmid, what = m.groups()
            key = "%s/%s" % (kind, vmid)
            if key not in fx.GUEST_CONFIG:
                return 404, None
            fw = fx.GUEST_FW.get(key, {})
            if what == "config":
                return 200, fx.GUEST_CONFIG[key]
            if what.startswith("firewall/"):
                part = what.split("/", 1)[1]
                return (200, fw.get(part)) if fw else (200, {} if part == "options" else [])
            if what == "agent/network-get-interfaces":
                return (200, fx.AGENT[key]) if key in fx.AGENT else (500, None)
            if what == "interfaces":
                return 200, fx.LXC_IFACES.get(key, [])
            if what == "rrddata":
                return 200, fx.RRD.get(key, [])
        return 404, None

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, data=None):
                body = json.dumps({"data": data}).encode() if status == 200 else b"{}"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _any(self):
                with fake._lock:
                    fake.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
                if self.command != "GET":
                    return self._send(405)
                if self.headers.get("Authorization") != "PVEAPIToken=%s=%s" % (TOKEN_ID, SECRET):
                    return self._send(401)
                for rx, status in fake.fail.items():
                    if re.search(rx, self.path):
                        return self._send(status)
                parts = urlsplit(self.path)
                status, data = fake.reply(parts.path, parse_qs(parts.query))
                return self._send(status, data)

            do_GET = do_POST = do_PUT = do_DELETE = _any

        return H
