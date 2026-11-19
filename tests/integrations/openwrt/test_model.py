"""Translation module of the OpenWrt family: ubus/UCI replies -> neutral model v2 (zones, rules in order, forwardings
and input policies, networks, wireless networks, clients, findings) for firewall3 and firewall4 releases."""
import json
from pathlib import Path

import pytest

from lanloom.firewall import neutral
from lanloom.integrations.openwrt import model

from . import fixtures as fx
from .fake_ubus import view


def values(secs):
    return {s[".name"]: s for s in view(secs)}


def raw_of(version="23.05", drop=(), air=True):
    raw = {k: values(f(version)) for k, f in fx.CONFIGS.items()}
    raw.update(version=version + ".5", board=fx.board(version), ifaces=fx.iface_dump(version), devices=fx.device_status(version),
               hints=fx.hints(), leases=fx.leases())
    if air:
        raw["air"] = {d: {"info": fx.iwinfo_info(d), "assoc": fx.iwinfo_assoc(d)["results"]}
                       for d in fx.AIR if "sta" not in d}
    raw = model.scrub(raw)
    for k in drop:
        raw.pop(k, None)
    return raw


def pol(st, name):
    return next(p for p in st["policies"] if p["name"] == name)


def zname(st, zid):
    return next(z["name"] for z in st["zones"] if z["id"] == zid) if zid != "*" else "*"


@pytest.mark.parametrize("version", ["21.02", "23.05"])
def test_state_is_valid_and_carries_no_secret_or_hardware_address(version):
    st = model.to_neutral(raw_of(version))
    neutral.validate(st)
    blob = json.dumps(st)
    assert not any(s in blob for s in fx.SECRETS)
    for mac in (fx.PHONE_MAC, fx.PRINTER_MAC, fx.ROUTER_MAC, fx.PLUG_MAC):
        assert mac not in blob and mac.replace(":", "") not in blob
    assert st["controller"]["firewall"] == ("fw3" if version == "21.02" else "fw4")
    assert st["controller"]["native_rollback"] is True and st["controller"]["class"] == "router"


def test_secrets_are_dropped_on_arrival_whatever_the_section():
    raw = model.scrub({"x": {".type": "wifi-iface", "key": "k", "sae_password": "p", "ssid": "Home", "encryption": "psk2"},
                       "y": {".type": "interface", "password": "p", "pincode": "1234", "private_key": "pk", "proto": "pppoe"},
                       "z": {".type": "tunnel_peer", "preshared_key": "psk", "public_key": "pub"},
                       "w": {"key1": "wep", "auth_secret": "radius", "identity": "user", "ca_cert": "/etc/ca"}})
    assert raw["x"] == {".type": "wifi-iface", "ssid": "Home", "encryption": "psk2"}
    assert raw["y"] == {".type": "interface", "proto": "pppoe"} and raw["z"] == {".type": "tunnel_peer", "public_key": "pub"}
    assert raw["w"] == {}


def test_zones_and_the_router_itself():
    st = model.to_neutral(raw_of())
    by = {z["name"]: z for z in st["zones"]}
    assert set(by) == {"lan", "wan", "iot", "guest", "This router"}
    assert by["wan"]["system"] == "external" and by["wan"]["masq"] and by["This router"]["system"] == "gateway"
    assert by["lan"]["interface"] and not by["This router"]["interface"]
    assert by["wan"]["internet"] and by["lan"]["internet"] and by["guest"]["internet"]


def test_rules_become_policies_in_configuration_order():
    st = model.to_neutral(raw_of())
    renew = pol(st, "Allow-DHCP-Renew")
    assert zname(st, renew["src"]["zone"]) == "wan" and zname(st, renew["dst"]["zone"]) == "This router"
    assert renew["proto"] == "udp" and renew["ports"] == "68" and renew["action"] == "allow" and renew["editable"]
    dns = pol(st, "IoT DNS")
    assert dns["proto"] == "tcp_udp" and dns["ports"] == "53"                 # UCI's default protocol is tcp + udp
    guests = pol(st, "Guests may not reach home")
    assert guests["action"] == "reject" and guests["proto"] == "any" and zname(st, guests["dst"]["zone"]) == "lan"
    broker = pol(st, "IoT to the broker")
    assert broker["dst"]["match"] == {"kind": "ip", "items": ["192.168.1.13"]} and not broker["editable"]
    assert not pol(st, "Allow-Ping")["editable"]                               # an ICMP type: shown, never rewritten
    assert pol(st, "Old test")["enabled"] is False
    rules = [p for p in st["policies"] if not p["builtin"]]
    assert [p["order"] for p in rules] == sorted(p["order"] for p in rules)
    assert all(p["order"] < model.FORWARD_ORDER for p in rules)


def test_unnamed_rules_marks_and_own_rules():
    st = model.to_neutral(raw_of())
    unnamed = next(p for p in st["policies"] if p["vendor_ref"].startswith("a:?"))
    assert unnamed["name"].startswith("rule ") and not unnamed["editable"]
    own = next(p for p in st["policies"] if p["vendor_ref"] == "s:" + fx.OWN)
    assert own["own"] and own["name"] == "IoT may not reach guests" and own["editable"]
    assert not any(p["own"] for p in st["policies"] if p is not own)
    assert any(r["area"] == "rule" and r["title"] == "Mark streaming" for r in st["raw"])     # a MARK rule decides nothing
    assert not any(p["name"] == "Mark streaming" for p in st["policies"])


def test_a_named_section_without_the_mark_is_not_own():
    raw = raw_of()
    raw["firewall"][fx.OWN]["name"] = "renamed by hand"
    st = model.to_neutral(raw)
    assert not next(p for p in st["policies"] if p["vendor_ref"] == "s:" + fx.OWN)["own"]


def test_forwardings_and_input_policies_follow_the_rules():
    st = model.to_neutral(raw_of())
    fwd = [p for p in st["policies"] if p.get("kind") == "forwarding"]
    assert {(zname(st, p["src"]["zone"]), zname(st, p["dst"]["zone"])) for p in fwd} == {("lan", "wan"), ("iot", "wan"), ("guest", "wan")}
    inp = {zname(st, p["src"]["zone"]): p["action"] for p in st["policies"] if p.get("kind") == "zone_input"}
    assert inp == {"lan": "allow", "wan": "reject", "iot": "reject", "guest": "reject"}
    assert all(p["builtin"] and not p["editable"] for p in fwd)


def test_zone_matrix_follows_first_match_and_the_global_forward_policy():
    st = model.to_neutral(raw_of())
    z = {x["name"]: x["id"] for x in st["zones"]}
    allow = {tuple(p) for p in st["allow"]}
    assert (z["lan"], z["wan"]) in allow and (z["guest"], z["wan"]) in allow
    assert (z["guest"], z["lan"]) not in allow and (z["wan"], z["lan"]) not in allow
    assert (z["lan"], z["This router"]) in allow and (z["wan"], z["This router"]) not in allow
    raw = raw_of()
    next(s for s in raw["firewall"].values() if s[".type"] == "defaults")["forward"] = "ACCEPT"
    st2 = model.to_neutral(raw)
    z2 = {x["name"]: x["id"] for x in st2["zones"]}
    assert (z2["wan"], z2["lan"]) in {tuple(p) for p in st2["allow"]}
    assert any(f["kind"] == "forward_accept" for f in st2["findings"])


def test_references_survive_a_commit_that_renames_anonymous_sections():
    a = model.to_neutral(raw_of())
    secs = fx.firewall()
    secs.insert(1, fx.sec("rule", anon=True, name="Inserted first", src="lan", target="ACCEPT"))   # every index shifts
    raw = raw_of()
    raw["firewall"] = values(secs)
    b = model.to_neutral(raw)
    ids_a = {p["name"]: p["id"] for p in a["policies"]}
    ids_b = {p["name"]: p["id"] for p in b["policies"]}
    assert all(ids_b[n] == i for n, i in ids_a.items())


def test_networks_vlans_and_address_pools():
    for version, lan_vlan in (("21.02", None), ("23.05", 1)):
        st = model.to_neutral(raw_of(version))
        n = {x["name"]: x for x in st["networks"]}
        assert n["lan"]["cidr"] == "192.168.1.0/24" and n["lan"]["gateway_ip"] == "192.168.1.1" and n["lan"]["vlan"] == lan_vlan
        assert n["iot"]["vlan"] == 30 and n["lan"]["dhcp"] == {"on": True, "range": ["192.168.1.100", "192.168.1.249"]}
        assert n["wan"]["purpose"] == "wan" and n["wan"]["cidr"] is None and n["guest"]["purpose"] == "guest"
        assert "loopback" not in n
        w = {x["name"]: x for x in st["wans"]}
        assert w["wan"]["status"] == "up" and w["wan"]["type"] == ("pppoe" if version == "21.02" else "dhcp")


def test_wireless_networks():
    st = model.to_neutral(raw_of())
    s = [(x["name"], x["bands"], x["security"], x["hidden"], x["clients"]) for x in st["ssids"]]
    assert s == [("Home", ["2.4ghz"], "wpa2_personal", False, 1), ("Home", ["5ghz"], "wpa2_wpa3_personal", False, 0),
                 ("Guests", ["2.4ghz"], "open", False, 0), ("Things", ["2.4ghz"], "wpa2_personal", True, 1)]
    assert all(x["on_air"] for x in st["ssids"]) and next(x for x in st["ssids"] if x["name"] == "Guests")["guest"]
    assert "Uplink" not in {x["name"] for x in st["ssids"]}                  # a client interface is no network of ours
    assert next(x for x in st["ssids"] if x["name"] == "Home" and x["bands"] == ["5ghz"])["wpa3"]
    old = model.to_neutral(raw_of("21.02"))
    assert [x["bands"] for x in old["ssids"]][:2] == [["2.4ghz"], ["5ghz"]]   # hwmode instead of band


@pytest.mark.parametrize("enc,want", [("none", "open"), ("owe", "owe"), ("psk2+ccmp", "wpa2_personal"), ("sae", "wpa3_personal"),
                                      ("sae-mixed", "wpa2_wpa3_personal"), ("wpa2+aes", "wpa2_enterprise"),
                                      ("wpa3", "wpa3_enterprise"), ("psk", "wpa_personal"), ("wep-open", "wep"), ("x", "other")])
def test_security_words_have_texts(enc, want):
    assert model._security(enc) == want
    texts = json.loads((Path(model.__file__).resolve().parents[2] / "i18n" / "en.json").read_text())
    assert "fw.sec." + want in texts


def test_clients_and_fixed_addresses():
    st = model.to_neutral(raw_of())
    by = {d["name"]: d for d in st["devices"]}
    assert by["phone"]["wired"] is False and by["phone"]["ssid"] == "Home" and by["phone"]["band"] == "2.4ghz" and by["phone"]["online"]
    assert by["printer"]["fixed"] and by["printer"]["fixed_ip"] == "192.168.1.40" and by["printer"]["wired"] is True
    assert by["smart-plug"]["network"] == next(n["id"] for n in st["networks"] if n["name"] == "iot")
    assert by["laptop"]["online"] is False                                     # only a host hint, no lease, not on air
    r = st["dhcp_reservations"][0]
    assert r["ip"] == "192.168.1.40" and r["client"] == by["printer"]["id"] and r["name"] == "printer"
    noair = model.to_neutral(raw_of(drop=("air",)))
    assert all(d["wired"] is None for d in noair["devices"])                  # unknown without the wireless state


def test_port_forwards_and_equipment():
    st = model.to_neutral(raw_of())
    f = st["port_forwards"][0]
    assert (f["name"], f["proto"], f["wan_port"], f["dst_ip"], f["dst_port"]) == ("Web server", "tcp", "8443", "192.168.1.20", "443")
    e = st["equipment"][0]
    assert e["name"] == "edge-router" and e["kind"] == "router" and e["firmware"] == "23.05.5" and e["mgmt_ip"] == "192.168.1.1"


def test_findings():
    st = model.to_neutral(raw_of())
    kinds = {f["kind"]: f for f in st["findings"]}
    assert kinds["admin_on_wan"]["params"] == {"zone": "wan", "services": "SSH"}
    assert kinds["ssid_open"]["params"] == {"ssid": "Guests"} and kinds["ssid_open"]["level"] == "info"   # isolated guests
    assert kinds["rules_disabled"]["params"] == {"n": 1}
    assert "wan_down" not in kinds                                             # wan6 down alone is no internet outage
    raw = raw_of()
    raw["firewall"]["x"] = {".type": "rule", ".anonymous": False, ".name": "x", ".index": 99, "name": "Open", "src": "wan",
                            "dest": "lan", "proto": "all", "target": "ACCEPT"}
    raw["ifaces"]["interface"][3]["up"] = False
    kinds = {f["kind"]: f for f in model.to_neutral(raw)["findings"]}
    assert kinds["wan_inbound_open"]["params"]["rule"] == "Open" and kinds["wan_down"]["params"] == {"wan": "wan"}


def test_missing_replies_leave_areas_empty():
    st = model.to_neutral({})
    neutral.validate(st)
    assert st["zones"] == [] and st["policies"] == [] and st["ssids"] == [] and st["devices"] == []
    st = model.to_neutral(raw_of(drop=("wireless", "dhcp", "leases", "hints", "air")))
    assert st["ssids"] == [] and st["dhcp_reservations"] == [] and st["policies"]


@pytest.mark.parametrize("v,want", [("23.05.5", (23, 5)), ("OpenWrt 24.10.0", (24, 10)), ("SNAPSHOT", (99, 0)), ("", ()),
                                    ("17.01.4", (17, 1))])
def test_versions(v, want):
    assert model.parse_version(v) == want
    assert model.native_rollback(v) is (None if not want else want >= (18, 6))


def test_ports_in_and_out():
    assert model.ports_text("80 443") == "80,443" and model.ports_text("8000:8080") == "8000-8080" and model.ports_text("ssh") is None
    assert model.ports_out("8000-8080") == "8000-8080" and model.ports_out("") == ""
    for bad in ("80,443", "0", "99999", "9-1"):
        with pytest.raises(ValueError):
            model.ports_out(bad)


def test_call_check():
    check = model.call_check({("uci", "get"): {"config": lambda v: v == "firewall"}, ("session", "login"): model.LOGIN_ARGS})
    sid = "f" * 32
    assert check([sid, "uci", "get", {"config": "firewall"}])
    assert not check([sid, "uci", "get", {"config": "network"}])
    assert not check([sid, "uci", "get", {"config": "firewall", "section": "x"}])
    assert not check([model.ZERO_SID, "uci", "get", {"config": "firewall"}])
    assert not check([sid, "session", "login", {"username": "a", "password": "b", "timeout": 900}])
    assert check([model.ZERO_SID, "session", "login", {"username": "a", "password": "b", "timeout": 900}])
    assert not check([model.ZERO_SID, "session", "login", {"username": "a b", "password": "b", "timeout": 900}])
    assert not check("x") and not check([sid, "uci", "get"]) and not check([sid.upper(), "uci", "get", {"config": "firewall"}])
