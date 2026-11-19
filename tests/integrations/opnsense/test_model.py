"""Translation module of the OPNsense family: replies of two releases -> neutral model v2 (pure, no network)."""
import json

import pytest

from lanloom.firewall import neutral, zonefw
from lanloom.integrations.opnsense import model, read

from . import fixtures as fx


def raw_of(version="24.7", drop=()):
    rp = fx.replies(version)
    got = {k: read.scrub(rp.get(p)) for k, p in read.PATHS.items() if k not in drop}
    raw = {"version": read.OpnsenseReader._version(got)}
    raw["filter"] = (got.get("filter") or {}).get("filter")
    raw["aliases"] = (got.get("aliases") or {}).get("alias")
    for k in ("interfaces", "rules", "groups", "vlans", "snat", "dnat", "kea", "dnsmasq", "routes", "gateways",
              "vpn_tunnel", "vpn_ipsec", "vpn_ssl"):
        raw[k] = got.get(k)
    return raw


def names(st):
    z = {x["id"]: x["name"] for x in st["zones"]}
    z["*"] = "*"
    return z


def pol(st, name):
    return next(p for p in st["policies"] if p["name"] == name)


@pytest.mark.parametrize("version", ["24.7", "25.7", "26.7"])
def test_state_is_valid_and_carries_no_secret_or_hardware_address(version):
    st = model.to_neutral(raw_of(version))
    neutral.validate(st)
    text = json.dumps(st)
    for bad in (fx.WG_PRIVATE, fx.PPPOE_SECRET, "02:4c:4c", fx.API_SECRET, "fw.example.invalid", "never"):
        assert bad not in text
    assert st["controller"]["version"] == version and st["controller"]["apis"] == ["rest"]


def test_interfaces_groups_and_the_firewall_become_zones():
    st = model.to_neutral(raw_of())
    z = {x["name"]: x for x in st["zones"]}
    assert set(z) == {"WAN", "LAN", "IoT", "Guests", "DMZ", "HomeNets", "This firewall"}
    assert z["WAN"]["system"] == "external" and z["This firewall"]["system"] == "gateway"
    assert z["LAN"]["interface"] and not z["HomeNets"]["interface"] and z["HomeNets"]["kind"] == "group"
    nets = {n["name"]: n for n in st["networks"]}
    assert nets["IoT"]["cidr"] == "192.168.30.0/24" and nets["IoT"]["vlan"] == 30 and nets["IoT"]["gateway_ip"] == "192.168.30.1"
    assert nets["WAN"]["cidr"] is None and nets["WAN"]["purpose"] == "wan"
    assert [w["name"] for w in st["wans"]] == ["WAN"] and st["wans"][0]["gateways"] == ["WAN_DHCP"]


def test_rules_become_policies_between_zones():
    st = model.to_neutral(raw_of())
    zn = names(st)
    p = pol(st, "IoT may not reach LAN")
    assert (zn[p["src"]["zone"]], zn[p["dst"]["zone"]], p["action"], p["editable"]) == ("IoT", "LAN", "block", True)
    p = pol(st, "IoT web")
    assert (zn[p["dst"]["zone"]], p["proto"], p["ports"]) == ("*", "tcp", "443")          # a well-known name as number
    p = pol(st, "Camera stream from outside")
    assert zn[p["dst"]["zone"]] == "DMZ" and p["ports"] == "554,8554" and not p["editable"]   # port alias resolved
    assert p["dst"]["match"] == {"kind": "ip", "items": ["192.168.70.20"]}
    p = pol(st, "Guests to the internet only")
    assert p["dst"]["match"]["opposite"] is True and p["dst"]["match"]["items"] == ["private_nets"] and not p["editable"]
    p = pol(st, "Admin from outside")
    assert zn[p["dst"]["zone"]] == "This firewall"
    own = pol(st, "Guests may not reach the DMZ")
    assert own["own"] and own["vendor_ref"] == fx.U["own_block"]
    assert not pol(st, "LAN to anywhere")["own"]


def test_evaluation_order_floating_then_interfaces_then_classic():
    st = model.to_neutral(raw_of("26.7"))
    order = {p["name"]: p["order"] for p in st["policies"]}
    assert order["Floating allow all"] < order["LAN to anywhere"] < order["IoT web"] < order["Default allow LAN to any rule"]
    classic = pol(st, "Guests may ask the firewall for names")
    assert classic["builtin"] and not classic["editable"] and names(st)[classic["src"]["zone"]] == "Guests"
    assert classic["ports"] == "53" and classic["proto"] == "tcp_udp"


def test_zone_matrix_follows_first_match():
    st = model.to_neutral(raw_of())
    zn = names(st)
    allow = {(zn[a], zn[b]) for a, b in st["allow"]}
    assert ("LAN", "DMZ") in allow and ("LAN", "WAN") in allow
    assert ("IoT", "LAN") not in allow and ("WAN", "LAN") not in allow
    v = zonefw.ask(st, "zone:" + model.zone_id("opt1"), "zone:" + model.zone_id("lan"))
    assert v["verdict"] in ("block", "depends") and (v["by"] or {}).get("name") in (None, "IoT may not reach LAN")
    v = zonefw.ask(st, "zone:" + model.zone_id("lan"), "zone:" + model.zone_id("opt3"))
    assert v["verdict"] == "allow" and v["by"]["name"] == "LAN to anywhere"


def test_findings():
    st = model.to_neutral(raw_of())
    kinds = {f["kind"]: f for f in st["findings"]}
    assert kinds["admin_on_wan"]["params"] == {"zone": "WAN", "services": "HTTPS"}
    assert kinds["rules_disabled"]["params"]["n"] == 2
    raw = raw_of()
    raw["filter"]["rules"]["rule"][fx.U["floating"]]["enabled"] = "1"
    raw["filter"]["rules"]["rule"][fx.U["old_test"]]["source_net"] = {"any": {"value": "any", "selected": 1}}
    raw["filter"]["rules"]["rule"][fx.U["old_test"]]["interface"] = fx.iface_opt("wan")
    raw["filter"]["rules"]["rule"][fx.U["old_test"]]["enabled"] = "1"
    raw["gateways"]["items"][0]["status"] = "down"
    kinds = {f["kind"]: f for f in model.to_neutral(raw)["findings"]}
    assert kinds["any_any"]["params"]["rule"] == "Floating allow all"
    assert kinds["wan_inbound_open"]["params"] == {"rule": "Old test rule", "zone": "DMZ"}
    assert kinds["wan_down"]["params"]["wan"] == "WAN_DHCP"


def test_nat_reservations_vpn_and_raw():
    st = model.to_neutral(raw_of("26.7"))
    f = st["port_forwards"][0]
    assert (f["name"], f["dst_ip"], f["wan_port"], f["dst_port"], f["proto"], f["enabled"]) == \
        ("Camera stream", "192.168.70.20", "8554", "554", "tcp", True)
    assert {r["ip"] for r in st["dhcp_reservations"]} == {"192.168.50.31", "192.168.70.20"}
    assert all(r["client"] is None for r in st["dhcp_reservations"])
    assert [(v["kind"], v["enabled"]) for v in st["vpn"]] == [("tunnel", True), ("ipsec", True), ("ssl", False)]
    areas = {r["area"] for r in st["raw"]}
    assert {"nat", "route", "rule_group", "vlan"} <= areas and all(r["editable"] is False for r in st["raw"])
    assert model.to_neutral(raw_of("24.7"))["port_forwards"] == []         # no destination NAT API before 26.1


def test_missing_replies_leave_areas_empty():
    st = model.to_neutral({})
    neutral.validate(st)
    assert st["zones"] == [] and st["policies"] == [] and st["findings"] == []
    st = model.to_neutral(raw_of(drop=("interfaces",)))
    assert {z["kind"] for z in st["zones"]} >= {"lan", "opt1", "wan"}                 # interfaces from the rule API's options
    assert st["policies"]


def test_rule_search_rows_stand_in_when_the_model_object_is_not_readable():
    raw = raw_of("24.7")
    raw["filter"] = None
    st = model.to_neutral(raw)
    assert {p["vendor_ref"] for p in st["policies"]} >= {fx.U["lan_any"], fx.U["own_block"]}


@pytest.mark.parametrize("version,native", [("OPNsense 23.7.12", True), ("24.1.10", True), ("26.1.6", True),
                                            ("26.7", False), ("OPNsense 27.1.a", False), ("", None), ("x", None)])
def test_native_rollback_per_release(version, native):
    assert model.native_rollback(version) is native


@pytest.mark.parametrize("spec,out", [("", ""), ("any", ""), ("443", "443"), ("8000:8080", "8000-8080"), ("https", "443"),
                                      ("cam_ports", "554,8554"), ("unknown_alias", None)])
def test_ports_in_plain_form(spec, out):
    aliases = {"cam_ports": {"type": "port", "content": ["554", "8554"]}}
    assert model.ports_text(spec, aliases) == out


def test_ports_out_writes_one_port_or_one_range():
    assert model.ports_out("") == "" and model.ports_out("443") == "443" and model.ports_out("8000-8080") == "8000:8080"
    with pytest.raises(ValueError):
        model.ports_out("80,443")


def test_rule_digest_keeps_rules_with_the_same_description_apart():
    raw = raw_of()
    rules = raw["filter"]["rules"]["rule"]
    rules[fx.U["iot_web"]]["description"] = "IoT may not reach LAN"
    dg = model.to_neutral(raw)["rule_digest"]
    assert sorted(d["name"] for d in dg if d["name"].startswith("IoT may not")) == ["IoT may not reach LAN (1)", "IoT may not reach LAN (2)"]
