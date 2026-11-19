"""Sample replies of the firewall's REST API, built after the API reference and the source of the releases (models
Filter.xml/Alias.xml, the overview and search controllers). Two shapes per release where they differ:

* ``24.7`` - option fields of ``.../get`` as ``{key: {"value": label, "selected": 0|1}}``, rule API with savepoint;
* ``26.7`` - the search of the rule API also lists the classic rules (``"legacy": true``), destination NAT has its own
  API, no savepoint any more.

Sample values only: documentation addresses (192.0.2.0/24, 203.0.113.0/24) on the internet side, 192.168.x.0/24
inside, locally administered hardware addresses (02:4c:4c:...). The keys and secrets below are test values.
"""
import copy

API_KEY = "dGVzdGtleXJlYWRvbmx5MDAwMDAwMDAwMDAwMDAwMA=="             # secret-scan: fake
API_SECRET = "c2VjcmV0dmFsdWVmb3J0ZXN0c29ubHkwMDAwMDAwMA=="          # secret-scan: fake
WRITE_KEY = "d3JpdGVrZXlmb3J0ZXN0c29ubHkwMDAwMDAwMDAwMDA="           # secret-scan: fake
WRITE_SECRET = "d3JpdGVzZWNyZXRmb3J0ZXN0c29ubHkwMDAwMDAwMDA="        # secret-scan: fake
WG_PRIVATE = "kPrivateKeyMaterialThatMustNeverLeave000000="          # secret-scan: fake
PPPOE_SECRET = "pppoe-password-never-copied"                         # secret-scan: fake

IFACES = [
    # identifier, description, device, addr4, vlan, link, gateways
    ("wan", "WAN", "igb0", "203.0.113.10/24", None, "dhcp", ["WAN_DHCP"]),
    ("lan", "LAN", "igb1", "192.168.50.1/24", None, "static", []),
    ("opt1", "IoT", "vlan0.30", "192.168.30.1/24", 30, "static", []),
    ("opt2", "Guests", "vlan0.80", "192.168.80.1/24", 80, "static", []),
    ("opt3", "DMZ", "igb2", "192.168.70.1/24", None, "static", []),
]
U = {k: "%s-0000-4000-8000-%012d" % (h, n) for n, (k, h) in enumerate([
    ("lan_any", "a0000001"), ("iot_broker", "a0000002"), ("iot_lan", "a0000003"), ("iot_web", "a0000004"),
    ("guest_out", "a0000005"), ("admin_wan", "a0000006"), ("cam_in", "a0000007"), ("old_test", "a0000008"),
    ("own_block", "a0000009"), ("floating", "a000000a"), ("group_dns", "a000000b")], start=1)}
PROTO_KEYS = ["any", "ICMP", "IGMP", "TCP", "UDP", "TCP/UDP", "GRE", "ESP"]


def opt(keys, selected, labels=None):
    sel = set(selected if isinstance(selected, (list, tuple, set)) else [selected])
    return {k: {"value": (labels or {}).get(k, k), "selected": 1 if k in sel else 0} for k in keys}


def iface_opt(selected):
    return opt([i[0] for i in IFACES] + ["homenets"], selected, {i[0]: i[1] for i in IFACES} | {"homenets": "HomeNets"})


def rule(desc, iface, *, action="pass", src="any", dst="any", proto="any", port="", enabled="1", seq=1, direction="in",
         dst_not="0", src_not="0", net_shape="option", sched="", gateway=""):
    def net(v):
        if net_shape == "plain":
            return v
        vals = v.split(",")
        return {x: {"value": x, "selected": 1} for x in vals}
    return {"enabled": enabled, "statetype": opt(["keep", "sloppy", "modulate", "synproxy", "none"], "keep"),
            "sequence": str(seq), "action": opt(["pass", "block", "reject"], action), "quick": "1", "interfacenot": "0",
            "interface": iface_opt(iface.split(",") if iface else []), "direction": opt(["in", "out"], direction),
            "ipprotocol": opt(["inet", "inet6", "inet46"], "inet"), "protocol": opt(PROTO_KEYS, proto),
            "source_net": net(src), "source_not": src_not, "source_port": "", "destination_net": net(dst),
            "destination_not": dst_not, "destination_port": port, "gateway": gateway, "log": "0", "sched": sched,
            "categories": {}, "description": desc}


def rules(net_shape="option"):
    r = {
        U["lan_any"]: rule("LAN to anywhere", "lan", src="lan", seq=10),
        U["iot_broker"]: rule("IoT to the broker", "opt1", src="opt1", dst="192.168.50.13", proto="TCP", port="8883", seq=20),
        U["iot_lan"]: rule("IoT may not reach LAN", "opt1", action="block", src="opt1", dst="lan", seq=30),
        U["iot_web"]: rule("IoT web", "opt1", src="opt1", proto="TCP", port="https", seq=40),
        U["guest_out"]: rule("Guests to the internet only", "opt2", src="opt2", dst="private_nets", dst_not="1", seq=50),
        U["admin_wan"]: rule("Admin from outside", "wan", dst="(self)", proto="TCP", port="443", seq=60),
        U["cam_in"]: rule("Camera stream from outside", "wan", dst="192.168.70.20", proto="TCP", port="cam_ports", seq=70),
        U["old_test"]: rule("Old test rule", "lan", src="lan", dst="opt3", enabled="0", seq=80),
        U["own_block"]: rule("[lanloom] Guests may not reach the DMZ", "opt2", action="block", src="opt2", dst="opt3", seq=90),
        U["floating"]: rule("Floating allow all", "", seq=5, enabled="0"),
        U["group_dns"]: rule("Inside may ask the firewall for names", "homenets", dst="(self)", proto="TCP/UDP", port="53", seq=15),
    }
    if net_shape == "plain":
        for v in r.values():
            for k in ("source_net", "destination_net"):
                v[k] = ",".join(x for x, o in v[k].items() if o["selected"])
    return r


def filter_get(net_shape="option") -> dict:
    return {"filter": {"rules": {"rule": rules(net_shape)},
                       "snatrules": {"rule": {"b0000001-0000-4000-8000-000000000001": {
                           "enabled": "1", "nonat": "0", "sequence": "100", "interface": iface_opt("wan"),
                           "description": "Guests out via WAN"}}},
                       "npt": {"rule": {}}, "onetoone": {"rule": {}}}}


def interfaces_info() -> dict:
    out = []
    for ident, desc, dev, addr, vlan, link, gws in IFACES:
        row = {"identifier": ident, "description": desc, "device": dev, "status": "up", "enabled": True, "link_type": link,
               "addr4": addr, "addr6": "", "ipv4": [{"ipaddr": addr}], "vlan_tag": str(vlan) if vlan else None,
               "gateways": gws, "macaddr": "02:4c:4c:00:01:%02x" % len(out), "is_physical": vlan is None,
               "config": {"identifier": ident, "if": dev, "descr": desc, "enable": "1",
                          "ipaddr": "dhcp" if link == "dhcp" else addr.split("/")[0]}}
        if ident == "wan":
            row["config"]["pppoe_password"] = PPPOE_SECRET          # never copied
        out.append(row)
    return {"total": len(out), "rowCount": len(out), "current": 1, "rows": out}


def aliases_get() -> dict:
    return {"alias": {"aliases": {"alias": {
        "c0000001-0000-4000-8000-000000000001": {"enabled": "1", "name": "cam_ports",
                                                  "type": opt(["host", "network", "port", "url"], "port"),
                                                  "content": {"554": {"value": "554", "selected": 1},
                                                              "8554": {"value": "8554", "selected": 1}},
                                                  "description": "Camera ports"},
        "c0000002-0000-4000-8000-000000000002": {"enabled": "1", "name": "private_nets",
                                                  "type": opt(["host", "network", "port", "url"], "network"),
                                                  "content": {"10.0.0.0/8": {"value": "10.0.0.0/8", "selected": 1},
                                                              "172.16.0.0/12": {"value": "172.16.0.0/12", "selected": 1},
                                                              "192.168.0.0/16": {"value": "192.168.0.0/16", "selected": 1}},
                                                  "description": "Private networks"}}}}}


def search(rows_) -> dict:
    return {"total": len(rows_), "rowCount": len(rows_), "current": 1, "rows": rows_}


def legacy_rows() -> list:
    return [{"legacy": True, "ref": "rule-lan-default", "enabled": "1", "action": "Pass", "interface": "LAN",
             "direction": "In", "ipprotocol": "IPv4", "protocol": "any", "source_net": "lan", "destination_net": "any",
             "description": "Default allow LAN to any rule", "sequence": "0"},
            {"legacy": True, "ref": "rule-guest-dns", "enabled": "1", "action": "Pass", "interface": "Guests",
             "direction": "In", "ipprotocol": "IPv4", "protocol": "TCP/UDP", "source_net": "opt2", "destination_net": "(self)",
             "destination_port": "53", "description": "Guests may ask the firewall for names", "sequence": "0"}]


def replies(version: str = "24.7") -> dict:
    """path -> JSON reply of every GET the read half asks, for one release."""
    v = {"24.7": "OPNsense 24.7.12_4-amd64", "26.7": "OPNsense 26.7.4-amd64", "25.7": "OPNsense 25.7.6-amd64"}[version]
    new = version >= "26.1"
    out = {
        "/api/diagnostics/system/systemInformation": {"name": "fw.example.invalid", "versions": [v, "FreeBSD 14.3-RELEASE-p2", "OpenSSL 3.0.17"]},
        "/api/core/firmware/status": {"product_version": v.split()[1].split("-")[0], "status": "none"},
        "/api/interfaces/overview/interfacesInfo": interfaces_info(),
        "/api/firewall/filter/get": filter_get("plain" if new else "option"),
        "/api/firewall/group/searchItem": search([{"uuid": "d0000001-0000-4000-8000-000000000001", "ifname": "HomeNets",
                                                  "members": "lan,opt1", "descr": "Inside networks"}]),
        "/api/firewall/alias/get": aliases_get(),
        "/api/interfaces/vlan_settings/searchItem": search([
            {"uuid": "e0000001-0000-4000-8000-000000000001", "if": "igb1", "tag": "30", "descr": "IoT", "vlanif": "vlan0.30"},
            {"uuid": "e0000002-0000-4000-8000-000000000002", "if": "igb1", "tag": "80", "descr": "Guests", "vlanif": "vlan0.80"}]),
        "/api/firewall/source_nat/searchRule": search([{"uuid": "b0000001-0000-4000-8000-000000000001", "enabled": "1",
                                                        "description": "Guests out via WAN", "interface": "WAN"}]),
        "/api/kea/dhcpv4/searchReservation": search([{"uuid": "f0000001-0000-4000-8000-000000000001", "subnet": "192.168.50.0/24",
                                                      "ip_address": "192.168.50.31", "hw_address": "02:4c:4c:00:00:31",
                                                      "hostname": "printer", "description": "Office printer"}]),
        "/api/routes/routes/searchroute": search([{"uuid": "f1000001-0000-4000-8000-000000000001", "network": "10.20.0.0/16",
                                                   "gateway": "LAB_GW", "descr": "Lab", "disabled": "0"}]),
        "/api/routes/gateway/status": {"items": [{"name": "WAN_DHCP", "address": "203.0.113.1", "status": "none",
                                                  "status_translated": "Online", "loss": "0.0 %", "delay": "4.1 ms"}],
                                       "status": "ok"},
        "/api/wireguard/server/searchServer": search([{"uuid": "f2000001-0000-4000-8000-000000000001", "enabled": "1",
                                                       "name": "Phones on the road", "privkey": WG_PRIVATE,
                                                       "pubkey": "cHVibGljLWtleS1zYW1wbGU=", "port": "51820"}]),
        "/api/ipsec/connections/searchConnection": search([{"uuid": "f3000001-0000-4000-8000-000000000001", "enabled": "1",
                                                            "description": "Holiday flat", "pre_shared_key": "never"}]),
        "/api/openvpn/instances/search": search([{"uuid": "f4000001-0000-4000-8000-000000000001", "enabled": "0",
                                                  "description": "Old tunnel", "role": "server"}]),
    }
    flt_rows = []
    for u, r in rules("plain").items():
        row = {"uuid": u, "enabled": r["enabled"], "sequence": r["sequence"], "description": r["description"],
               "action": "Pass" if r["action"]["pass"]["selected"] else "Block",
               "interface": ",".join(i for i, o in r["interface"].items() if o["selected"])}
        flt_rows.append(row)
    if new:
        flt_rows += legacy_rows()
        out["/api/firewall/d_nat/searchRule"] = search([{"uuid": "f5000001-0000-4000-8000-000000000001", "disabled": "0",
                                                         "descr": "Camera stream", "protocol": "TCP", "interface": "wan",
                                                         "destination.port": "8554", "target": "192.168.70.20",
                                                         "local-port": "554", "source.network": "any"}])
        out["/api/dnsmasq/settings/searchHost"] = search([{"uuid": "f6000001-0000-4000-8000-000000000001", "host": "camera",
                                                           "domain": "home.arpa", "ip": "192.168.70.20",
                                                           "hwaddr": "02:4c:4c:00:00:70"}])
    out["/api/firewall/filter/searchRule"] = search(flt_rows)
    return copy.deepcopy(out)
