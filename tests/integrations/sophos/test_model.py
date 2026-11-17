"""Translation of the XML API replies into the neutral model (pure, no network)."""
import json

import pytest

from lanloom.firewall import neutral
from lanloom.integrations.sophos import model

from . import fixtures as fx


def raw_of(reply=fx.REPLY):
    meta, ents = model.parse_reply(reply)
    return {"api_version": meta["api_version"], "entities": ents}


@pytest.fixture(scope="module")
def raw():
    return raw_of()


@pytest.fixture(scope="module")
def st(raw):
    return model.to_neutral(raw)


def zone(st, name):
    return next(z for z in st["zones"] if z["name"] == name)


def pols(st, name):
    return [p for p in st["policies"] if p["name"] == name]


# ---------------------------------------------------------------------------- overall
def test_state_validates(st):
    neutral.validate(st)
    assert st["v"] == 2 and st["controller"] == {"version": fx.API_VERSION, "apis": ["xml"]}
    assert st["devices"] == [] and st["ports"] == [] and st["ssids"] == []


def test_zones(st):
    assert {z["name"] for z in st["zones"]} == {"LAN", "WAN", "DMZ", "VPN", "IoT"}
    assert zone(st, "LAN")["system"] == "internal" and zone(st, "WAN")["system"] == "external"
    assert zone(st, "DMZ")["system"] == "dmz" and zone(st, "VPN")["system"] == "vpn"
    assert zone(st, "IoT")["system"] == "internal" and zone(st, "IoT")["builtin"] is False
    assert zone(st, "WAN")["admin_services"] == ["https"]
    assert zone(st, "LAN")["admin_services"] == ["https", "ssh"] and zone(st, "DMZ")["admin_services"] == []


def test_networks(st):
    nets = {n["name"]: n for n in st["networks"]}
    assert set(nets) == {"Port1", "Port2", "Port3", "Port1.20", "Port1:0"}      # Port4 has IPv4 switched off
    lan = nets["Port1"]
    assert lan["cidr"] == "192.168.10.0/24" and lan["zone"] == zone(st, "LAN")["id"] and lan["dhcp"] is True
    assert lan["management"] is True and lan["purpose"] == "lan"
    vlan = nets["Port1.20"]
    assert vlan["vlan"] == 20 and vlan["zone"] == zone(st, "IoT")["id"] and vlan["cidr"] == "192.168.20.0/24" and vlan["dhcp"] is True
    alias = nets["Port1:0"]
    assert alias["zone"] == zone(st, "LAN")["id"] and alias["cidr"] == "192.168.11.0/24" and alias["kind"] == "alias"
    wan = nets["Port2"]
    assert wan["cidr"] is None and wan["purpose"] == "wan" and wan["zone"] == zone(st, "WAN")["id"] and wan["management"] is False
    assert nets["Port3"]["dhcp"] is False
    assert [w["name"] for w in st["wans"]] == ["Port2"] and st["wans"][0]["type"] == "dhcp"


def test_rule_with_two_source_zones_becomes_two_policies(st):
    ps = pols(st, "Inside to DMZ")
    assert len(ps) == 2 and {p["vendor_ref"] for p in ps} == {"Inside to DMZ"}
    assert {p["src"]["zone"] for p in ps} == {zone(st, "LAN")["id"], zone(st, "VPN")["id"]}
    assert {p["dst"]["zone"] for p in ps} == {zone(st, "DMZ")["id"]}
    assert len({p["id"] for p in ps}) == 2 and all(p["scheduled"] for p in ps)
    assert len({p["vendor_ref"] for p in st["policies"]}) == 8     # the business application rule is not a policy


def test_any_and_empty_zones_become_star(st):
    ps = pols(st, "Old test rule")
    assert len(ps) == 1 and ps[0]["src"]["zone"] == "*" and ps[0]["dst"]["zone"] == "*" and ps[0]["enabled"] is False
    assert model.zone_id("Any") == "*" and model.zone_id("") == "*" and model.zone_id(None) == "*"


def test_service_group_resolves_to_ports_and_proto(st):
    p = pols(st, "Things to internet (web)")[0]
    # Web (HTTP, HTTPS: tcp) plus DNS (tcp and udp) -> mixed protocols
    assert p["proto"] == "any" and "53" in p["ports"] and "80" in p["ports"] and "443" in p["ports"]
    only_web = model.to_neutral(_with_rule(services=["Web"]))
    q = pols(only_web, "t")[0]
    assert q["proto"] == "tcp" and q["ports"] == "80, 443"


def test_dns_alone_is_proto_any_with_ports():
    q = pols(model.to_neutral(_with_rule(services=["DNS"])), "t")[0]
    assert q["proto"] == "any" and q["ports"] == "53"


def test_rule_without_services_is_any_without_ports(st):
    p = pols(st, "LAN to internet")[0]
    assert p["proto"] == "any" and p["ports"] == "" and p["action"] == "allow" and p["order"] == 0


def test_address_objects_are_put_in_words(st):
    p = pols(st, "Things may use MQTT")[0]
    assert p["dst"]["match"] == {"kind": "ip", "items": ["192.168.10.20"]}
    assert p["proto"] == "tcp" and p["ports"] == "8883" and p["group"] == "Things"
    assert pols(st, "Staff browsing")[0]["users"] == 1


def test_drop_rule_is_block(st):
    assert pols(st, "Drop things to DMZ")[0]["action"] == "block"


def test_derive_allow(st):
    lan, wan, iot = (zone(st, n)["id"] for n in ("LAN", "WAN", "IoT"))
    allow = [tuple(a) for a in st["allow"]]
    assert (lan, wan) in allow and (wan, lan) in allow
    assert (iot, wan) not in allow            # only a port-limited rule: not a whole-zone verdict
    assert zone(st, "LAN")["internet"] is True and zone(st, "IoT")["internet"] is False


def test_findings(st):
    kinds = {f["kind"]: f for f in st["findings"]}
    assert set(kinds) == {"admin_on_wan", "wan_inbound_open", "rules_disabled"}
    assert kinds["admin_on_wan"]["level"] == "alert" and kinds["admin_on_wan"]["params"] == {"zone": "WAN", "services": "HTTPS"}
    assert kinds["wan_inbound_open"]["params"]["rule"] == "Remote desk from outside" and kinds["wan_inbound_open"]["params"]["zone"] == "LAN"
    assert kinds["rules_disabled"]["params"] == {"n": 1} and kinds["rules_disabled"]["level"] == "info"
    assert len({f["id"] for f in st["findings"]}) == len(st["findings"])


def test_any_any_finding_for_an_enabled_open_rule():
    s = model.to_neutral(_with_rule(zones=False, services=[], name="Everything"))
    f = [f for f in s["findings"] if f["kind"] == "any_any"]
    assert len(f) == 1 and f[0]["level"] == "warn" and f[0]["params"] == {"rule": "Everything"}


def test_no_any_any_for_a_disabled_or_limited_rule():
    assert not [f for f in model.to_neutral(_with_rule(zones=False, services=[], enabled=False))["findings"] if f["kind"] == "any_any"]
    assert not [f for f in model.to_neutral(_with_rule(zones=False, services=["Web"]))["findings"] if f["kind"] == "any_any"]


def test_port_forward(st):
    assert len(st["port_forwards"]) == 1
    f = st["port_forwards"][0]
    assert f["name"] == "Web server" and f["dst_ip"] == "172.16.30.10" and f["wan_port"] == "443" and f["dst_port"] == "443"
    assert f["proto"] == "tcp" and f["enabled"] is True and f["source_limited"] is False


def test_source_nat_and_business_rule_go_to_raw(st):
    areas = {(r["area"], r["title"]) for r in st["raw"]}
    assert ("nat", "Default SNAT IPv4") in areas
    assert ("rule_businessapplication", "Mail protection") in areas
    assert ("rule_group", "Things") in areas and any(r["area"] == "route" for r in st["raw"])
    assert all(r["editable"] is False for r in st["raw"])
    assert not [p for p in st["policies"] if p["name"] == "Mail protection"]


def test_vpn(st):
    assert {(v["name"], v["kind"], v["enabled"]) for v in st["vpn"]} == {("Office tunnel", "ipsec", True), ("Spare", "ipsec", False),
                                                                         ("Road warriors", "ssl", True)}


def test_dhcp_reservation_never_holds_the_hardware_address(st):
    assert len(st["dhcp_reservations"]) == 1
    r = st["dhcp_reservations"][0]
    assert r["ip"] == "192.168.10.30" and r["name"] == "printer" and r["client"] is None
    dump = json.dumps(st).lower()
    assert "02:00:5e" not in dump and "02005e" not in dump and "02-00-5e" not in dump


def test_ids_are_not_mac_like_and_stable(raw, st):
    again = model.to_neutral(raw_of())
    assert json.dumps(again, sort_keys=True) == json.dumps(st, sort_keys=True)
    for key in ("zones", "networks", "policies", "port_forwards", "dhcp_reservations", "wans", "vpn", "raw", "findings"):
        for it in st[key]:
            assert not neutral.MAC_LIKE.match(it["id"])


def test_no_brand_name_leaks_into_the_state(st):
    assert "sophos" not in json.dumps(st).lower()


def test_rule_digest(st):
    d = {r["name"]: r for r in st["rule_digest"]}
    assert len(d) == 8 and d["Old test rule"]["enabled"] is False and d["LAN to internet"]["allow"] is True
    assert d["Drop things to DMZ"]["allow"] is False
    assert all(len(r["sig"]) == 16 for r in d.values())
    assert len({r["sig"] for r in d.values()}) == 8


def test_digest_signature_follows_the_effect_not_the_name_or_order(raw):
    base = {r["name"]: r["sig"] for r in model.to_neutral(raw)["rule_digest"]}
    s2 = model.to_neutral(_with_rule(services=["HTTP"], name="x1"))
    s3 = model.to_neutral(_with_rule(services=["HTTP"], name="x2"))
    assert {r["name"]: r["sig"] for r in s2["rule_digest"]}["x1"] == {r["name"]: r["sig"] for r in s3["rule_digest"]}["x2"]
    assert base["LAN to internet"]


# ---------------------------------------------------------------------------- empty and odd input
def test_empty_raw_gives_an_empty_valid_state():
    for raw in ({}, {"entities": {}}, None, {"api_version": "", "entities": {"Zone": []}}):
        s = model.to_neutral(raw)
        neutral.validate(s)
        assert s["zones"] == [] and s["policies"] == [] and s["findings"] == [] and s["rule_digest"] == [] and s["allow"] == []


def test_rule_naming_an_unlisted_zone_adds_a_placeholder_zone():
    s = model.to_neutral({"entities": {"FirewallRule": [{"Name": "r", "Status": "Enable", "NetworkPolicy": {
        "Action": "Accept", "SourceZones": {"Zone": "Ghost"}, "DestinationZones": {"Zone": "LAN"}}}]}})
    neutral.validate(s)
    ids = {z["id"] for z in s["zones"]}
    assert s["policies"][0]["src"]["zone"] in ids and s["policies"][0]["dst"]["zone"] in ids


def test_many_zone_pairs_are_capped():
    zs = {"Zone": [{"Name": "z%d" % i} for i in range(10)]}
    r = {"Name": "big", "Status": "Enable", "NetworkPolicy": {"Action": "Accept", "SourceZones": {"Zone": ["z%d" % i for i in range(10)]},
                                                              "DestinationZones": {"Zone": ["z%d" % i for i in range(10)]}}}
    s = model.to_neutral({"entities": dict(zs, FirewallRule=[r])})
    assert len(s["policies"]) == model.MAX_PAIRS


# ---------------------------------------------------------------------------- parse_reply
def test_parse_reply_meta_and_entities():
    meta, ents = model.parse_reply(fx.REPLY)
    assert meta["api_version"] == fx.API_VERSION and meta["login"] == "Authentication Successful" and meta["status"] is None
    assert len(ents["Zone"]) == 5 and ents["Zone"][0]["Name"] == "LAN"
    assert ents["Zone"][0]["ApplianceAccess"]["AdminServices"]["HTTPS"] == "HTTPS"
    assert isinstance(ents["Services"][2]["ServiceDetails"]["ServiceDetail"], list)       # repeated tag becomes a list


@pytest.mark.parametrize("data", [
    b'<!DOCTYPE r [<!ENTITY a "b">]><Response><Login/></Response>',
    b'<?xml version="1.0"?><!DOCTYPE Response><Response/>',
    b'<Response><!ENTITY x "y"></Response>',
    b"<Response>" + b" " * 5000 + b'<!doctype x></Response>',
    b"not xml at all", b"<Response><Zone></Response>", b"", b"   ", None, "<Response/>", 5,
    b"<Other/>", b"<html><body/></html>",
])
def test_parse_reply_refuses(data):
    with pytest.raises(model.ReplyError):
        model.parse_reply(data)


def test_entity_with_only_status_is_recorded_in_meta_not_as_entity():
    meta, ents = model.parse_reply(fx.wrap(fx.EMPTY_NAT))
    assert meta["entity_status"] == {"NATRule": "529"} and "NATRule" not in ents
    s = model.to_neutral({"entities": ents})
    assert s["port_forwards"] == []


def test_root_status_is_kept_in_meta():
    meta, ents = model.parse_reply(fx.IP_REFUSED)
    assert meta["status_code"] == "534" and "requester IP" in meta["status"] and meta["login"] is None and ents == {}
    meta, _ = model.parse_reply(fx.wrap("", login=fx.LOGIN_FAIL))
    assert meta["login"] == "Authentication Failure"


# ---------------------------------------------------------------------------- helpers
def _with_rule(zones=True, services=("Web",), enabled=True, name="t"):
    """Fixture entities plus one extra enabled accept rule (any/any when ``zones`` is False)."""
    r = _rule(name, zones, services, enabled)
    raw = raw_of()
    ents = dict(raw["entities"])
    ents["FirewallRule"] = [r]                  # only the extra rule, so the checks see nothing else
    return {"api_version": raw["api_version"], "entities": ents}


def _rule(name, zones, services, enabled):
    part = {"Action": "Accept", "Schedule": "All The Time"}
    if zones:
        part["SourceZones"] = {"Zone": "LAN"}
        part["DestinationZones"] = {"Zone": "WAN"}
    if services:
        part["Services"] = {"Service": list(services)}
    return {"Name": name, "Status": "Enable" if enabled else "Disable", "PolicyType": "Network", "NetworkPolicy": part}


# ---------------------------------------------------------------------------- shapes of the published API reference
REFERENCE_SHAPES = fx.wrap(
    '<Zone><Name>LAN</Name><Type>LAN</Type></Zone><Zone><Name>WAN</Name><Type>WAN</Type></Zone>'
    '<Interface><Name>Port2</Name><Hardware>Port2</Hardware><NetworkZone>WAN</NetworkZone><IPv4Configuration>Enable</IPv4Configuration>'
    '<IPv4Assignment>PPPoE</IPv4Assignment><Username>dsl-user</Username><Password>pppoe-secret-77</Password></Interface>'
    '<VPNIPSecConnection><Configuration><Name>Branch</Name><ConnectionType>SiteToSite</ConnectionType><Policy>IKEv2</Policy>'
    '<AuthenticationType>PresharedKey</AuthenticationType><PresharedKey>psk-secret-55</PresharedKey><Status>Active</Status>'
    '</Configuration></VPNIPSecConnection>'
    '<SSLVPNPolicy><TunnelPolicy><Name>Remote staff</Name><PolicyMembers><Member>staff</Member></PolicyMembers></TunnelPolicy></SSLVPNPolicy>'
    '<DHCPServer><Name>LAN DHCP</Name><Interface>Port1</Interface><StaticLease><Lease><HostName>nas</HostName>'
    '<MACAddress>02:00:5e:10:00:09</MACAddress><IPAddress>192.168.10.9</IPAddress></Lease></StaticLease></DHCPServer>',
    login="<Login><status>Authetication Successful</status></Login>")


def test_reference_shapes_are_read_and_secrets_are_dropped():
    meta, ents = model.parse_reply(REFERENCE_SHAPES)
    st = model.to_neutral({"api_version": meta["api_version"], "entities": ents})
    neutral.validate(st)
    vpn = {v["name"]: v for v in st["vpn"]}
    assert vpn["Branch"]["kind"] == "ipsec" and vpn["Branch"]["type"] == "SiteToSite" and vpn["Branch"]["enabled"] is True
    assert vpn["Remote staff"]["kind"] == "ssl"
    assert [(r["name"], r["ip"]) for r in st["dhcp_reservations"]] == [("nas", "192.168.10.9")]
    assert st["wans"][0]["type"] == "pppoe"
    dump = json.dumps(st).lower()
    for secret in ("pppoe-secret-77", "psk-secret-55", "dsl-user", "02:00:5e:10:00:09", "presharedkey"):
        assert secret not in dump, secret


def test_login_text_of_the_reference_counts_as_signed_in():
    meta, _ = model.parse_reply(REFERENCE_SHAPES)
    assert "success" in meta["login"].lower()
