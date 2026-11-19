"""A writable fake of the classic API for the write half: client settings change on ``PUT rest/user/<id>``.

It behaves like the controller software does (checked against a self-hosted application): an address outside the
network named in the same request or an address of another client is refused (400, ``api.err.InvalidFixedIP`` /
``api.err.DuplicateFixedIP``), an unknown client id too (``api.err.IdInvalid``); a network id that does not exist,
an empty or very long name are stored without a word - the write half has to check those itself.

Faults on order: ``fail_put`` (the n-th PUT answers 500), ``drop_put`` (the n-th PUT is applied and then the
connection is cut without a reply - network loss mid-write), ``ignore_put`` (200, nothing stored), ``stuck``
(a moved wireless client stays in its old network).

Zone firewall (``v2/api/site/default/firewall/zone`` and ``firewall-policies``), as measured on a self-hosted
application after the switch to zones: a zone update needs its ``_id`` in the body (else 500) and takes no other
fields than ``_id``, ``name`` and ``network_ids`` (400); a network belongs to exactly one zone (adding it to one takes
it out of the other; taken out of a zone of its own it goes back to the internal zone); a default zone cannot be
deleted (400); deleting a zone puts its networks back into the internal zone **and deletes every rule that names it,
without a word**; rule bodies are taken only whole (400 for a missing field); new rules go to the end of their zone
pair (``index`` 10000 upwards); ``batch-reorder`` numbers the rules of one pair again.

Switch ports: ``PUT rest/device/<id>`` takes only ``port_overrides`` and replaces the whole list (an entry left out is
gone); the port table itself stays as it was until the switch provisions again (it does not here, as with a switch
that is offline). ``POST cmd/devmgr`` with ``power-cycle`` counts the cycles of a port; a port without power answers
``api.err.PoeNotSupported``.

Wireless networks: ``POST/PUT/DELETE rest/wlanconf`` like networks (a new id, a merge, gone); a protected network without
a password is refused (``api.err.XPassphraseRequired``), a name another SSID has too (``api.err.WlanNameExist``).
``POST cmd/stamgr`` with ``block-sta`` / ``unblock-sta`` sets ``blocked`` on the client's user record; an unknown
address answers ``api.err.UnknownStation``; any other command ``api.err.InvalidCommand``."""
import copy
import ipaddress
import json
import re

from . import fixtures as fx
from .fake_controller import CSRF, PW, USER, FakeController

OID = re.compile(r"\A[0-9a-f]{24}\Z")
U_OFFICE, U_PRINTER, U_PHONE, U_GUEST = ("5f00000000000000000007%02x" % i for i in range(1, 5))
D_GW, D_SW, D_AP = ("5f0000000000000000000c%02x" % i for i in range(1, 4))
# what a switch keeps per port besides the settings the write half may change (copied from a recorded console)
KEEP = {"autoneg": True, "egress_rate_limit_kbps_enabled": False, "isolation": False, "port_security_enabled": False,
        "port_security_mac_address": [], "setting_preference": "auto", "stormctrl_bcast_enabled": False,
        "stormctrl_bcast_rate": 100, "voice_networkconf_id": ""}
FIELDS = {"name", "use_fixedip", "fixed_ip", "network_id", "virtual_network_override_enabled", "virtual_network_override_id"}


def writable_data() -> dict:
    """The fixture network with user ids and user records for the wireless clients."""
    data = fx.classic()
    users = data["user"]["data"]
    users[0]["_id"], users[1]["_id"] = U_OFFICE, U_PRINTER
    users[0]["is_wired"] = True
    users.append({"_id": U_PHONE, "mac": fx.MAC_PHONE, "name": "Phone", "is_wired": False})
    users.append({"_id": U_GUEST, "mac": fx.MAC_GUEST, "is_wired": False, "is_guest": True})
    for d, did in zip(data["device"]["data"], (D_GW, D_SW, D_AP)):
        d["_id"] = did
    data["networkconf"]["data"][0]["attr_hidden_id"] = "LAN"          # the site's default network
    sw = data["device"]["data"][1]
    sw["port_overrides"] = [dict(KEEP, port_idx=3, name="Port 3", op_mode="switch", poe_mode="auto", forward="all",
                                 tagged_vlan_mgmt="auto"),
                            dict(KEEP, port_idx=4, name="Lab desk", forward="native", native_networkconf_id=fx.C_NET_LAB,
                                 tagged_vlan_mgmt="block_all")]
    sw["port_table"][1]["poe_mode"] = "auto"
    sw["port_table"].insert(3, {"port_idx": 5, "name": "Port 5", "up": True, "speed": 1000, "media": "GE", "port_poe": True,
                                "poe_enable": False, "poe_mode": "off", "forward": "all"})
    sw["port_table"].insert(4, {"port_idx": 6, "name": "Port 6", "up": False, "media": "GE", "port_poe": True,
                                "poe_enable": False, "poe_mode": "pasv24", "forward": "all"})
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
        self.device_puts = []            # (device id, body) of every switch write
        self.cycles = []                 # (mac, port) of every power cycle
        self.stamgr = []                 # (cmd, mac) of every block / unblock
        self.fail_device = 0             # the n-th switch write answers 500
        self.fail_config = 0             # the n-th network / port forward write answers 500
        self.drop_config = 0             # ... is applied and then the connection is cut
        self.classic.setdefault("zone", [])
        self.classic.setdefault("policies", [])

    # ------------------------------------------------------------------ data
    def users(self) -> list:
        return self.classic["user"]["data"]

    def user(self, uid):
        return next((u for u in self.users() if u.get("_id") == uid), None)

    def sta(self, mac):
        return next((c for c in self.classic["sta"]["data"] if c.get("mac") == mac), None)

    def net(self, cid):
        return next((n for n in self.classic["networkconf"]["data"] if n.get("_id") == cid), None)

    # ------------------------------------------------------------------ zone firewall
    def zones(self) -> list:
        return self.classic["zone"]

    def zone(self, zid):
        return next((z for z in self.zones() if z.get("_id") == zid), None)

    def policies(self) -> list:
        return self.classic["policies"]

    def policy(self, pid):
        return next((p for p in self.policies() if p.get("_id") == pid), None)

    def _internal(self):
        return next((z for z in self.zones() if z.get("zone_key") == "internal" or z.get("name") == "Internal"), None)

    def _place(self, zone, network_ids):
        """A network sits in exactly one zone."""
        for z in self.zones():
            if z is not zone:
                z["network_ids"] = [n for n in z.get("network_ids") or [] if n not in network_ids]
        dropped = [n for n in zone.get("network_ids") or [] if n not in network_ids]
        zone["network_ids"] = list(network_ids)
        inner = self._internal()
        if inner is not None and inner is not zone:
            inner["network_ids"] = list(inner.get("network_ids") or []) + [n for n in dropped if n not in inner.get("network_ids", [])]
        for n in self.classic["networkconf"]["data"]:
            for z in self.zones():
                if n.get("_id") in (z.get("network_ids") or []):
                    n["firewall_zone_id"] = z["_id"]

    POLICY_NEEDED = ("name", "enabled", "action", "ip_version", "schedule", "source", "destination", "protocol")

    def write_v2(self, method, area, oid, body):
        """(status, reply) of the zone firewall API like the controller."""
        if area == "firewall/zone":
            if method == "POST":
                if set(body) - {"name", "network_ids"}:
                    return 400, {"errorCode": 400, "message": "JSON parse error"}
                self.next_id += 1
                new = {"_id": "5f0000000000000000000a%02x" % self.next_id, "external_id": "zone-%d" % self.next_id,
                       "name": body.get("name"), "network_ids": [], "default_zone": False, "attr_no_edit": False, "zone_key": None}
                if not self.ignore_put:
                    self.zones().append(new)
                    self._place(new, list(body.get("network_ids") or []))
                return 201, copy.deepcopy(new)
            z = self.zone(oid)
            if z is None:
                return 404, {"code": "api.err.FirewallZoneNotFound", "errorCode": 404}
            if method == "DELETE":
                if z.get("default_zone"):
                    return 400, {"code": "api.err.DefaultFirewallZoneDeleteNotAllowed", "errorCode": 400}
                if not self.ignore_put:
                    self._place(z, [])
                    self.zones().remove(z)
                    self.classic["policies"] = [p for p in self.policies() if z["_id"] not in
                                                ((p.get("source") or {}).get("zone_id"), (p.get("destination") or {}).get("zone_id"))]
                return 204, None
            if set(body) - {"_id", "name", "network_ids"}:
                return 400, {"errorCode": 400, "message": "JSON parse error: Unrecognized field"}
            if body.get("_id") != oid or z.get("attr_no_edit"):
                return 500, None
            if not self.ignore_put:
                z["name"] = body.get("name", z["name"])
                self._place(z, list(body.get("network_ids") or []))
            return 200, copy.deepcopy(z)
        if area == "firewall-policies/batch-reorder":
            src, dst = body.get("source_zone_id"), body.get("destination_zone_id")
            order = list(body.get("before_predefined_ids") or []) + list(body.get("after_predefined_ids") or [])
            if not self.ignore_put:
                for i, pid in enumerate(order):
                    p = self.policy(pid)
                    if p is not None and (p["source"]["zone_id"], p["destination"]["zone_id"]) == (src, dst):
                        p["index"] = 10000 + i
            return 200, [copy.deepcopy(p) for p in self.policies() if (p["source"]["zone_id"], p["destination"]["zone_id"]) == (src, dst)]
        if method == "POST":
            if any(k not in body for k in self.POLICY_NEEDED):
                return 400, {"errorCode": 400, "message": "Validation failed"}
            pair = (body["source"].get("zone_id"), body["destination"].get("zone_id"))
            mine = [p.get("index", 0) for p in self.policies() if (p["source"].get("zone_id"), p["destination"].get("zone_id")) == pair
                    and not p.get("predefined")]
            self.next_id += 1
            new = dict(copy.deepcopy(body), _id="5f0000000000000000000b%02x" % self.next_id, predefined=False,
                       index=(max(mine) + 1) if mine else 10000)
            if not self.ignore_put:
                self.policies().append(new)
            return 201, copy.deepcopy(new)
        p = self.policy(oid)
        if p is None:
            return 404, {"code": "api.err.FirewallPolicyNotFound", "errorCode": 404}
        if method == "DELETE":
            if not self.ignore_put:
                self.policies().remove(p)
            return 204, None
        if any(k not in body for k in self.POLICY_NEEDED):
            return 400, {"errorCode": 400, "message": "Validation failed"}
        if not self.ignore_put:
            p.update(copy.deepcopy(body))
        return 200, copy.deepcopy(p)

    def classic_reply(self, path):
        m = re.fullmatch(re.escape(self.prefix) + r"/v2/api/site/default/firewall-policies/([0-9a-f]{24})", path)
        if m:
            p = self.policy(m.group(1))
            return copy.deepcopy(p) if p else None
        m = re.fullmatch(re.escape(self.prefix) + r"/api/s/default/rest/(user|networkconf|portforward|wlanconf)/([0-9a-f]{24})", path)
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
        if area == "wlanconf" and method in ("POST", "PUT"):
            o = None if method == "POST" else self.obj(area, oid)
            merged = dict(o or {}, **body)
            if merged.get("security") == "wpapsk" and not merged.get("x_passphrase"):
                return 400, {"meta": {"rc": "error", "msg": "api.err.XPassphraseRequired"}, "data": []}
            if any(w.get("name") == merged.get("name") and w is not o for w in rows):
                return 400, {"meta": {"rc": "error", "msg": "api.err.WlanNameExist"}, "data": []}
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

    def device(self, did):
        return next((d for d in self.classic["device"]["data"] if d.get("_id") == did), None)

    def put_device(self, did, body):
        d = self.device(did)
        if d is None:
            return 400, {"meta": {"rc": "error", "msg": "api.err.IdInvalid"}, "data": []}
        if not isinstance(body, dict) or set(body) - {"port_overrides"} or not isinstance(body.get("port_overrides"), list):
            return 400, {"meta": {"rc": "error", "msg": "api.err.InvalidPayload"}, "data": []}
        self.device_puts.append((did, copy.deepcopy(body)))
        if len(self.device_puts) == self.fail_device:
            return 500, {"meta": {"rc": "error", "msg": "api.err.Internal"}}
        if not self.ignore_put:
            d["port_overrides"] = copy.deepcopy(body["port_overrides"])
        return 200, {"meta": {"rc": "ok"}, "data": [copy.deepcopy(d)]}

    def sta_cmd(self, body):
        if not isinstance(body, dict) or body.get("cmd") not in ("block-sta", "unblock-sta"):
            return 400, {"meta": {"rc": "error", "msg": "api.err.InvalidCommand"}, "data": []}
        u = next((x for x in self.users() if x.get("mac") == body.get("mac")), None)
        if u is None:
            return 400, {"meta": {"rc": "error", "msg": "api.err.UnknownStation"}, "data": []}
        self.stamgr.append((body["cmd"], body["mac"]))
        if not self.ignore_put:
            u["blocked"] = body["cmd"] == "block-sta"
            c = self.sta(body["mac"])
            if c is not None and u["blocked"]:
                self.classic["sta"]["data"].remove(c)      # a blocked client drops off at once
        return 200, {"meta": {"rc": "ok"}, "data": []}

    def power_cycle(self, body):
        if not isinstance(body, dict) or body.get("cmd") != "power-cycle":
            return 400, {"meta": {"rc": "error", "msg": "api.err.InvalidCommand"}, "data": []}
        d = next((x for x in self.classic["device"]["data"] if x.get("mac") == body.get("mac")), None)
        p = next((x for x in (d or {}).get("port_table") or [] if x.get("port_idx") == body.get("port_idx")), None)
        if p is None or not p.get("port_poe"):
            return 400, {"meta": {"rc": "error", "msg": "api.err.PoeNotSupported"}, "data": []}
        self.cycles.append((body["mac"], body["port_idx"]))
        return 200, {"meta": {"rc": "ok"}, "data": []}

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
            def _v2(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}") if n else None
                except ValueError:
                    body = None
                self.record(body)
                m = re.fullmatch(re.escape(fake.prefix) + r"/v2/api/site/default/(firewall/zone|firewall-policies/batch-reorder|"
                                 r"firewall-policies)(?:/([0-9a-f]{24}))?", self.path)
                cookie = self.headers.get("Cookie") or ""
                sid = next((c.split("=", 1)[1] for c in cookie.split("; ") if c.split("=", 1)[0] in ("TOKEN", "unifises")), None)
                if sid not in fake.sessions:
                    return self.reply(401, {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}})
                if fake.require_csrf and self.headers.get("X-CSRF-Token") != CSRF:
                    return self.reply(403, {"meta": {"rc": "error", "msg": "api.err.Csrf"}})
                if not m:
                    return self.reply(404, {})
                fake.config_writes.append((method, m.group(1), m.group(2)))
                count = len(fake.config_writes)
                if fake.fail_config and count == fake.fail_config:
                    return self.reply(500, {"errorCode": 500})
                status, reply = fake.write_v2(method, m.group(1), m.group(2), body or {})
                if fake.drop_config and count == fake.drop_config:
                    self.close_connection = True
                    self.wfile.flush()
                    self.connection.close()
                    return None
                if status == 204:
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self.reply(status, reply)

            def _config(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}") if n else None
                except ValueError:
                    body = None
                self.record(body)
                m = re.fullmatch(re.escape(fake.prefix) + r"/api/s/default/rest/(networkconf|portforward|wlanconf)(?:/([0-9a-f]{24}))?", self.path)
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

            def _device(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}") if n else None
                except ValueError:
                    body = None
                self.record(body)
                cookie = self.headers.get("Cookie") or ""
                sid = next((c.split("=", 1)[1] for c in cookie.split("; ") if c.split("=", 1)[0] in ("TOKEN", "unifises")), None)
                if sid not in fake.sessions:
                    return self.reply(401, {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}})
                if fake.require_csrf and self.headers.get("X-CSRF-Token") != CSRF:
                    return self.reply(403, {"meta": {"rc": "error", "msg": "api.err.Csrf"}})
                if method == "POST" and self.path.endswith("/cmd/stamgr"):
                    return self.reply(*fake.sta_cmd(body))
                if method == "POST":
                    return self.reply(*fake.power_cycle(body))
                return self.reply(*fake.put_device(self.path.rsplit("/", 1)[1], body))

            def do_POST(self):
                if self.path in (fake.prefix + "/api/s/default/cmd/devmgr", fake.prefix + "/api/s/default/cmd/stamgr"):
                    return self._device("POST")
                if re.fullmatch(re.escape(fake.prefix) + r"/api/s/default/rest/(networkconf|portforward|wlanconf)", self.path):
                    return self._config("POST")
                if self.path.startswith(fake.prefix + "/v2/api/site/default/firewall"):
                    return self._v2("POST")
                return base.do_POST(self)

            def do_DELETE(self):
                if self.path.startswith(fake.prefix + "/v2/api/site/default/firewall"):
                    return self._v2("DELETE")
                return self._config("DELETE")

            def do_PUT(self):
                if self.path.startswith(fake.prefix + "/v2/api/site/default/firewall"):
                    return self._v2("PUT")
                if re.fullmatch(re.escape(fake.prefix) + r"/api/s/default/rest/device/[0-9a-f]{24}", self.path):
                    return self._device("PUT")
                if re.fullmatch(re.escape(fake.prefix) + r"/api/s/default/rest/(networkconf|portforward|wlanconf)/[0-9a-f]{24}", self.path):
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


__all__ = ["WritableFake", "writable_data", "USER", "PW", "U_OFFICE", "U_PRINTER", "U_PHONE", "U_GUEST", "D_SW", "KEEP"]
