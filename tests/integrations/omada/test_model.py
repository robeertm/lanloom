"""The translation module: collected Open API replies -> neutral model v2 and the topology observation."""
import json

import pytest

from lanloom.firewall import neutral
from lanloom.integrations.omada import model

from . import fixtures as fx


def raw_of() -> dict:
    d = fx.site_data()
    return {"info": fx.INFO, "site": fx.SITES[0], "devices": d["devices"], "clients": d["clients"], "networks": d["networks"],
            "profiles": d["profiles"], "ssids": [dict(s, wlan="Default") for s in d["ssids"]["wl-default"]],
            "acl_gateway": d["acl_gateway"], "acl_switch": d["acl_switch"], "acl_eap": d["acl_eap"],
            "wans": [dict(w, gateway=fx.GW.lower().replace("-", ":")) for w in d["wans"]],
            "switch_info": {fx.CORE: d["switch_info"][fx.CORE]}, "switch_stat": {fx.CORE: d["switch_stat"][fx.CORE]}}


@pytest.fixture()
def st():
    return model.to_neutral(raw_of())


def by_name(rows, name):
    return next(r for r in rows if r["name"] == name)


def test_state_is_valid_neutral_v2_and_has_every_area(st):
    neutral.validate(st)
    assert st["v"] == 2 and st["zones"] == [] and st["policies"] == [] and st["allow"] == []
    assert {k: len(st[k]) for k in ("equipment", "devices", "networks", "ssids", "wans", "ports", "raw", "dhcp_reservations")} == {
        "equipment": 5, "devices": 7, "networks": 3, "ssids": 4, "wans": 1, "ports": 8, "raw": 3, "dhcp_reservations": 1}
    assert st["controller"] == {"version": fx.VERSION, "class": "access_controller", "apis": ["official"], "site": "Default"}


def test_no_hardware_address_serial_or_secret_leaves_the_model(st):
    text = json.dumps(st).lower()
    for needle in ("02-00-5e", "02:00:5e", "3c-00-5e", "3c:00:5e", "sn-never-copied", fx.CLIENT_SECRET):
        assert needle not in text


def test_wireless_passphrase_of_a_detail_reply_is_never_copied():
    raw = raw_of()
    raw["ssids"][0]["pskSetting"] = {"securityKey": "correct-horse-battery"}
    raw["ssids"][0]["securityKey"] = "correct-horse-battery"
    assert "correct-horse" not in json.dumps(model.to_neutral(raw))


def test_equipment_kinds_status_and_uplinks(st):
    eq = {e["name"]: e for e in st["equipment"]}
    assert [eq[n]["kind"] for n in ("Edge gateway", "Core switch", "Hall AP")] == ["router", "switch", "ap"]
    assert eq["Edge gateway"]["online"] and eq["Edge gateway"]["uplink"] is None
    assert eq["Desk switch"]["online"] is False and eq["Desk switch"]["pending"] is False
    assert eq["New AP"]["online"] is False and eq["New AP"]["pending"] is True and eq["New AP"]["uplink"] is None
    assert eq["Core switch"]["uplink"] == {"device": eq["Edge gateway"]["id"], "port": 5, "medium": None}
    assert eq["Core switch"]["stats"] == {"cpu": 4, "mem": 41, "uptime_s": 10 * 86400 + 3 * 3600 + 4 * 60 + 5, "clients": 3}
    assert eq["Hall AP"]["stats"]["clients"] == 3 and eq["Hall AP"]["model"] == "AP-650" and eq["Hall AP"]["firmware"] == "1.3.1"


def test_switch_ports_link_speed_poe_profile_neighbour_and_uplink(st):
    core = by_name(st["equipment"], "Core switch")["id"]
    ports = {p["idx"]: p for p in st["ports"] if p["device"] == core}
    assert sorted(ports) == list(range(1, 9))
    assert ports[3]["link"] and ports[3]["speed"] == 1000 and ports[3]["poe"] == {"capable": True, "on": True, "watts": None}
    assert ports[3]["lldp_neighbor"] == by_name(st["equipment"], "Hall AP")["id"] and ports[3]["infra"] is True
    assert ports[6]["speed"] == 100 and ports[6]["profile_name"] == "IoT ports"
    assert ports[6]["native_network"] == by_name(st["networks"], "IoT")["id"] and ports[6]["native_vlan"] == 20
    assert ports[1]["link"] is False and ports[1]["speed"] is None
    assert [i for i, p in ports.items() if p["is_uplink"]] == [5]
    assert ports[1]["profile_name"] == "All" and ports[1]["native_vlan"] is None          # VLAN 1 is the default


def test_clients_wired_wireless_parents_and_flags(st):
    eq = {e["name"]: e["id"] for e in st["equipment"]}
    nas, printer, camera = (by_name(st["devices"], n) for n in ("NAS", "Printer", "Camera"))
    assert nas["wired"] and nas["parent"] == eq["Core switch"] and nas["port"] == "4" and nas["alone_on_port"] is True
    assert nas["fixed"] and nas["fixed_ip"] == "192.168.10.20" and nas["network"] == by_name(st["networks"], "LAN")["id"]
    assert printer["alone_on_port"] is False and camera["alone_on_port"] is False            # two clients behind port 6
    assert camera["network"] == by_name(st["networks"], "IoT")["id"]
    gw = by_name(st["devices"], "Wired at gateway")
    assert gw["parent"] == eq["Edge gateway"] and gw["wired"]
    phone, guest, tablet = (by_name(st["devices"], n) for n in ("Phone", "Guest laptop", "Blocked tablet"))
    assert not phone["wired"] and phone["parent"] == eq["Hall AP"] and phone["ssid"] == "Home" and phone["band"] == "5ghz"
    assert phone["private_address"] is True and phone["port"] is None
    assert guest["guest"] and guest["band"] == "2.4ghz" and guest["network"] == by_name(st["networks"], "Guest")["id"]
    assert tablet["blocked"] and tablet["band"] == "6ghz"
    assert st["dhcp_reservations"][0]["client"] == nas["id"] and st["dhcp_reservations"][0]["ip"] == "192.168.10.20"


def test_networks_cidr_vlan_dhcp_and_management(st):
    lan, iot, guest = (by_name(st["networks"], n) for n in ("LAN", "IoT", "Guest"))
    assert (lan["cidr"], lan["vlan"], lan["gateway_ip"]) == ("192.168.10.0/24", None, "192.168.10.1")
    assert lan["dhcp"] == {"on": True, "range": ["192.168.10.100", "192.168.10.199"]} and lan["management"] is True
    assert (iot["cidr"], iot["vlan"]) == ("192.168.20.0/24", 20) and iot["management"] is False
    assert guest["cidr"] == "192.168.30.0/24" and guest["vlan"] == 30 and guest["dhcp"] == {"on": False, "range": None}


def test_ssids_bands_security_vlan_and_visibility(st):
    home, iot, guests, lab = (by_name(st["ssids"], n) for n in ("Home", "IoT", "Guests", "Lab"))
    assert home["bands"] == ["2.4ghz", "5ghz"] and home["security"] == "wpa_personal" and home["network"] is None
    assert iot["bands"] == ["2.4ghz"] and iot["network"] == by_name(st["networks"], "IoT")["id"] and iot["vlan"] == 20
    assert guests["security"] == "open" and guests["guest"] and guests["bands"] == ["2.4ghz", "5ghz", "6ghz"]
    assert lab["hidden"] is True and lab["security"] == "wpa_enterprise"


def test_only_wan_mode_ports_become_internet_connections(st):
    assert [(w["name"], w["type"], w["up"]) for w in st["wans"]] == [("WAN1", "dhcp", True)]
    assert st["wans"][0]["device"] == by_name(st["equipment"], "Edge gateway")["id"]


def test_access_rules_are_shown_never_editable_and_digested(st):
    assert all(r["area"] == "acl" and r["editable"] is False for r in st["raw"])
    names = {d["name"]: d for d in st["acl_digest"]}
    assert set(names) == {"Block IoT to LAN", "Allow all", "#1"}                              # a rule without a description: its number
    assert names["Block IoT to LAN"]["allow"] is False and names["Block IoT to LAN"]["enabled"] is True
    assert names["Allow all"]["allow"] is True and names["Allow all"]["enabled"] is False
    assert {d["scope"] for d in st["acl_digest"]} == {"gateway", "switch"}
    assert len({d["sig"] for d in st["acl_digest"]}) == 3


def test_digest_signature_changes_with_what_the_rule_does_not_with_its_name():
    a, b = raw_of(), raw_of()
    b["acl_gateway"][0]["description"] = "Renamed"
    c = raw_of()
    c["acl_gateway"][0]["destinationIds"] = [fx.NET_GUEST]
    sig = lambda r: model.to_neutral(r)["acl_digest"][0]["sig"]
    assert sig(a) == sig(b) and sig(a) != sig(c)


def test_findings_are_plain_and_ordered(st):
    kinds = [(f["kind"], f["level"]) for f in st["findings"]]
    assert kinds == [("device_offline", "warn"), ("ssid_open", "info"), ("acls_off", "info")]
    assert st["findings"][0]["params"] == {"device": "Desk switch"} and st["findings"][2]["params"] == {"n": 1}


def test_gateway_offline_is_an_alert_and_a_down_internet_connection_too():
    raw = raw_of()
    raw["devices"][0]["status"] = 0
    raw["wans"][0]["internetState"] = 0
    kinds = [(f["kind"], f["level"]) for f in model.to_neutral(raw)["findings"]]
    assert kinds[:2] == [("wan_down", "alert"), ("device_offline", "alert")]


def test_open_non_guest_wireless_network_is_a_warning():
    raw = raw_of()
    raw["ssids"][0]["security"] = 0
    f = [f for f in model.to_neutral(raw)["findings"] if f["kind"] == "ssid_open"]
    assert {(x["params"]["ssid"], x["level"]) for x in f} == {("Home", "warn"), ("Guests", "info")}


def test_empty_and_odd_input_never_raises():
    for raw in (None, {}, {"devices": "x", "clients": [None, 3, {}], "networks": [{"id": "n"}], "ssids": [[]]}):
        st = model.to_neutral(raw)
        neutral.validate(st)
        assert st["controller"]["version"] == ""


def test_repeated_rows_keep_ids_unique():
    raw = raw_of()
    raw["devices"].append(dict(raw["devices"][0]))
    raw["clients"].append(dict(raw["clients"][0]))
    raw["networks"].append(dict(raw["networks"][0]))
    neutral.validate(model.to_neutral(raw))


@pytest.mark.parametrize("value,expected", [("1day(s) 2h 3m 4s", 93784), ("3h 5m", 11100), ("12s", 12), (3600, 3600), ("", None), (None, None)])
def test_uptime_text(value, expected):
    assert model.uptime_s(value) == expected


def test_topology_event_has_the_controller_shape():
    ev = model.topology_event(raw_of(), 1700.0)
    assert ev["type"] == "topology" and ev["source"] == "controller" and ev["t"] == 1700.0
    roles = {d["name"]: d["role"] for d in ev["devices"]}
    assert roles == {"Edge gateway": "router", "Core switch": "switch", "Desk switch": "switch", "Hall AP": "ap", "New AP": "ap"}
    core = next(d for d in ev["devices"] if d["name"] == "Core switch")
    assert core["mac"] == "02:00:5e:20:00:02" and core["uplink"] == {"mac": "02:00:5e:20:00:01", "port": "5", "own_port": None, "medium": None}
    wired = next(c for c in ev["clients"] if c["name"] == "NAS")
    assert wired == {"mac": "3c:00:5e:30:00:01", "ip": "192.168.10.20", "name": "NAS", "medium": "wired",
                     "parent_mac": "02:00:5e:20:00:02", "port": "4", "band": None, "ssid": None}
    radio = next(c for c in ev["clients"] if c["name"] == "Phone")
    assert radio["medium"] == "radio" and radio["band"] == "5ghz" and radio["ssid"] == "Home" and radio["port"] is None
    assert len(ev["clients"]) == 7


def test_topology_skips_clients_without_a_parent_and_unknown_uplinks():
    raw = raw_of()
    raw["clients"].append({"mac": "3C-00-5E-30-00-99", "name": "Orphan", "ip": "192.168.10.99", "wireless": True})
    raw["devices"][3]["uplinkDeviceMac"] = "02-00-5E-99-99-99"                 # an uplink the site does not list
    ev = model.topology_event(raw, 1.0)
    assert all(c["name"] != "Orphan" for c in ev["clients"])
    assert next(d for d in ev["devices"] if d["name"] == "Hall AP")["uplink"] is None


def test_signal_of_wireless_clients_comes_from_the_client_list_in_whole_dbm():
    ev = model.topology_event(raw_of(), 1.0)
    by = {c["name"]: c for c in ev["clients"]}
    assert by["Phone"]["signal"] == -58                       # ``rssi`` in dBm; the percentage ``signalLevel`` is never used
    assert by["Guest laptop"]["signal"] is None                # -104 is no plausible reading and is dropped
    assert "signal" not in by["NAS"]                           # a wired client has none
