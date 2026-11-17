"""A writable fake of the classic API for the write half: client settings change on ``PUT rest/user/<id>``.

It behaves like the controller software does (checked against a self-hosted application): an address outside the
network named in the same request or an address of another client is refused (400, ``api.err.InvalidFixedIP`` /
``api.err.DuplicateFixedIP``), an unknown client id too (``api.err.IdInvalid``); a network id that does not exist,
an empty or very long name are stored without a word - the write half has to check those itself.

Faults on order: ``fail_put`` (the n-th PUT answers 500), ``drop_put`` (the n-th PUT is applied and then the
connection is cut without a reply - network loss mid-write), ``ignore_put`` (200, nothing stored), ``stuck``
(a moved wireless client stays in its old network)."""
import copy
import ipaddress
import json
import re

from . import fixtures as fx
from .fake_controller import CSRF, PW, USER, FakeController

OID = re.compile(r"\A[0-9a-f]{24}\Z")
U_OFFICE, U_PRINTER, U_PHONE, U_GUEST = ("5f00000000000000000007%02x" % i for i in range(1, 5))
FIELDS = {"name", "use_fixedip", "fixed_ip", "network_id", "virtual_network_override_enabled", "virtual_network_override_id"}


def writable_data() -> dict:
    """The fixture network with user ids and user records for the wireless clients."""
    data = fx.classic()
    users = data["user"]["data"]
    users[0]["_id"], users[1]["_id"] = U_OFFICE, U_PRINTER
    users[0]["is_wired"] = True
    users.append({"_id": U_PHONE, "mac": fx.MAC_PHONE, "name": "Phone", "is_wired": False})
    users.append({"_id": U_GUEST, "mac": fx.MAC_GUEST, "is_wired": False, "is_guest": True})
    by_mac = {u["mac"]: u["_id"] for u in users}
    for i, c in enumerate(data["sta"]["data"]):
        if c["mac"] in by_mac:
            c["user_id"] = by_mac[c["mac"]]
        else:                            # an online client without a user record of its own
            c["user_id"] = c["_id"] = "5f00000000000000000008%02x" % i
    return data


class WritableFake(FakeController):
    def __init__(self, style="console", data=None, **kw):
        super().__init__(style=style, official=False, data=data if data is not None else writable_data(), **kw)
        self.self_reply = fx.classic_wrap([{"name": USER, "site_role": "admin", "is_super": False}])
        self.puts = []                   # (user id, body)
        self.fail_put = 0
        self.drop_put = 0
        self.ignore_put = False
        self.stuck = False
        self.require_csrf = style == "console"
        self.next_id = 0
        self.config_writes = []          # (method, area, id) of every network / port forward write
        self.fail_config = 0             # the n-th network / port forward write answers 500
        self.drop_config = 0             # ... is applied and then the connection is cut

    # ------------------------------------------------------------------ data
    def users(self) -> list:
        return self.classic["user"]["data"]

    def user(self, uid):
        return next((u for u in self.users() if u.get("_id") == uid), None)

    def sta(self, mac):
        return next((c for c in self.classic["sta"]["data"] if c.get("mac") == mac), None)

    def net(self, cid):
        return next((n for n in self.classic["networkconf"]["data"] if n.get("_id") == cid), None)

    def classic_reply(self, path):
        m = re.fullmatch(re.escape(self.prefix) + r"/api/s/default/rest/(user|networkconf|portforward)/([0-9a-f]{24})", path)
        if m:
            area, oid = m.groups()
            u = self.user(oid) if area == "user" else self.obj(area, oid)
            return fx.classic_wrap([copy.deepcopy(u)]) if u else {"meta": {"rc": "error", "msg": "api.err.IdInvalid"}, "data": []}
        return super().classic_reply(path)

    def obj(self, area, oid):
        return next((o for o in self.classic.setdefault(area, fx.classic_wrap([]))["data"] if o.get("_id") == oid), None)

    def write_obj(self, method, area, oid, body):
        """networkconf / portforward like the controller: a new id on POST, a merge on PUT, gone on DELETE; a VLAN or
        a subnet another network has is refused (``api.err.VlanUsed`` / ``api.err.IpSubnetConflict``)."""
        rows = self.classic.setdefault(area, fx.classic_wrap([]))["data"]
        if method == "POST":
            if area == "networkconf":
                for n in rows:
                    if body.get("vlan_enabled") and str(n.get("vlan")) == str(body.get("vlan")) and n.get("vlan_enabled", True):
                        return 400, {"meta": {"rc": "error", "msg": "api.err.VlanUsed"}, "data": []}
                    if n.get("ip_subnet") and ipaddress.ip_interface(n["ip_subnet"]).network.overlaps(ipaddress.ip_interface(body["ip_subnet"]).network):
                        return 400, {"meta": {"rc": "error", "msg": "api.err.IpSubnetConflict"}, "data": []}
            self.next_id += 1
            new = dict(body, _id="5f00000000000000000009%02x" % self.next_id, site_id="site")
            if not self.ignore_put:
                rows.append(new)
            return 200, {"meta": {"rc": "ok"}, "data": [copy.deepcopy(new)]}
        o = self.obj(area, oid)
        if o is None:
            return 400, {"meta": {"rc": "error", "msg": "api.err.IdInvalid"}, "data": []}
        if method == "DELETE":
            if not self.ignore_put:
                rows.remove(o)
            return 200, {"meta": {"rc": "ok"}, "data": []}
        if not self.ignore_put:
            o.update(body)
        return 200, {"meta": {"rc": "ok"}, "data": [copy.deepcopy(o)]}

    def put_user(self, uid, body):
        """Returns (status, reply) like the controller."""
        u = self.user(uid)
        if u is None or not isinstance(body, dict):
            return 400, {"meta": {"rc": "error", "msg": "api.err.IdInvalid"}, "data": []}
        merged = dict(u, **body)
        if "fixed_ip" in body and body["fixed_ip"] != "":
            try:
                ip = ipaddress.ip_address(body["fixed_ip"])
            except ValueError:
                return 400, {"meta": {"rc": "error", "msg": "api.err.InvalidFixedIP"}, "data": []}
            net = self.net(body.get("network_id") or merged.get("network_id"))
            if net and net.get("ip_subnet") and ip not in ipaddress.ip_interface(net["ip_subnet"]).network:
                return 400, {"meta": {"rc": "error", "msg": "api.err.InvalidFixedIP"}, "data": []}
        if merged.get("use_fixedip") and merged.get("fixed_ip"):
            for other in self.users():
                if other is not u and other.get("use_fixedip") and other.get("fixed_ip") == merged["fixed_ip"]:
                    return 400, {"meta": {"rc": "error", "msg": "api.err.DuplicateFixedIP"}, "data": [{"fixed_ip": other["fixed_ip"]}]}
        self.puts.append((uid, dict(body)))
        if self.ignore_put:
            return 200, {"meta": {"rc": "ok"}, "data": [{"_id": uid}]}
        u.update(body)
        c = self.sta(u.get("mac"))
        if c is not None:
            for k in FIELDS & set(body):
                c[k] = body[k]
            if not self.stuck and not c.get("is_wired"):
                if u.get("virtual_network_override_enabled") and u.get("virtual_network_override_id"):
                    c["network_id"] = u["virtual_network_override_id"]
                if u.get("use_fixedip") and u.get("fixed_ip"):
                    c["ip"] = u["fixed_ip"]
        return 200, {"meta": {"rc": "ok"}, "data": [{k: u.get(k) for k in ("_id", "mac", "name", "site_id") if k in u}]}

    # ------------------------------------------------------------------ server
    def _handler(self):
        base = super()._handler()
        fake = self

        class H(base):
            def _config(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}") if n else None
                except ValueError:
                    body = None
                self.record(body)
                m = re.fullmatch(re.escape(fake.prefix) + r"/api/s/default/rest/(networkconf|portforward)(?:/([0-9a-f]{24}))?", self.path)
                cookie = self.headers.get("Cookie") or ""
                sid = next((c.split("=", 1)[1] for c in cookie.split("; ") if c.split("=", 1)[0] in ("TOKEN", "unifises")), None)
                if sid not in fake.sessions:
                    return self.reply(401, {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}})
                if fake.require_csrf and self.headers.get("X-CSRF-Token") != CSRF:
                    return self.reply(403, {"meta": {"rc": "error", "msg": "api.err.Csrf"}})
                if not m or (method == "POST") == bool(m.group(2)):
                    return self.reply(404, {"meta": {"rc": "error"}})
                fake.config_writes.append((method, m.group(1), m.group(2)))
                count = len(fake.config_writes)
                if fake.fail_config and count == fake.fail_config:
                    return self.reply(500, {"meta": {"rc": "error", "msg": "api.err.Internal"}})
                status, reply = fake.write_obj(method, m.group(1), m.group(2), body or {})
                if fake.drop_config and count == fake.drop_config:
                    self.close_connection = True
                    self.wfile.flush()
                    self.connection.close()
                    return None
                return self.reply(status, reply)

            def do_POST(self):
                if re.fullmatch(re.escape(fake.prefix) + r"/api/s/default/rest/(networkconf|portforward)", self.path):
                    return self._config("POST")
                return base.do_POST(self)

            def do_DELETE(self):
                return self._config("DELETE")

            def do_PUT(self):
                if re.fullmatch(re.escape(fake.prefix) + r"/api/s/default/rest/(networkconf|portforward)/[0-9a-f]{24}", self.path):
                    return self._config("PUT")
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    body = None
                self.record(body)
                cookie = self.headers.get("Cookie") or ""
                sid = next((c.split("=", 1)[1] for c in cookie.split("; ") if c.split("=", 1)[0] in ("TOKEN", "unifises")), None)
                if fake.always_401 or fake.expire_next > 0 or sid not in fake.sessions:
                    with fake._lock:
                        if fake.expire_next > 0:
                            fake.expire_next -= 1
                        fake.sessions.clear()
                    return self.reply(401, {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}})
                if fake.require_csrf and self.headers.get("X-CSRF-Token") != CSRF:
                    return self.reply(403, {"meta": {"rc": "error", "msg": "api.err.Csrf"}})
                m = re.fullmatch(re.escape(fake.prefix) + r"/api/s/default/rest/user/([0-9a-f]{24})", self.path)
                if not m:
                    return self.reply(404, {"meta": {"rc": "error"}})
                count = len(fake.puts) + 1
                if fake.fail_put and count == fake.fail_put:
                    fake.puts.append((m.group(1), "failed"))
                    return self.reply(500, {"meta": {"rc": "error", "msg": "api.err.Internal"}})
                status, reply = fake.put_user(m.group(1), body)
                if fake.drop_put and count == fake.drop_put:
                    self.close_connection = True
                    self.wfile.flush()
                    self.connection.close()
                    return None
                return self.reply(status, reply)

        return H


__all__ = ["WritableFake", "writable_data", "USER", "PW", "U_OFFICE", "U_PRINTER", "U_PHONE", "U_GUEST"]
