"""The translation module: sample replies -> neutral model v2 and the topology observation."""
import copy
import json
import re

import pytest

from lanloom.firewall import neutral
from lanloom.integrations.mikrotik import model

from . import fixtures as fx


def raw_of():
    return fx.raw_copy()


def state(raw=None):
    return model.to_neutral(raw if raw is not None else raw_of())


def by(rows, key, value):
    return next(r for r in rows if r[key] == value)


# ---------------------------------------------------------------------------- shape
def test_state_is_a_valid_neutral_model_v2_with_the_family_marker():
    st = state()
    neutral.validate(st)
    assert st["v"] == 2 and st["zones"] == [] and st["policies"] == [] and st["ssids"] == [] and st["allow"] == []
    assert st["controller"] == {"version": fx.VERSION, "class": "router_switch", "apis": ["official"], "site": "edge-router"}
    assert {"equipment", "ports", "networks", "devices", "dhcp_reservations", "port_forwards", "wans", "raw",
            "rule_digest", "findings"} <= set(st)


def test_empty_and_garbage_input_gives_an_empty_valid_state():
    for raw in (None, {}, "x", {"interfaces": "nope", "leases": [1, None, "a"], "resource": []}):
        st = model.to_neutral(raw)
        neutral.validate(st)
        assert st["networks"] == [] and st["devices"] == [] and st["ports"] == []


def test_ids_are_opaque_unique_and_never_hardware_addresses_or_names():
    st = state()
    text = json.dumps(st).lower()
    for key in ("equipment", "ports", "networks", "devices", "dhcp_reservations", "port_forwards", "wans", "raw", "findings"):
        ids = [o["id"] for o in st[key]]
        assert len(ids) == len(set(ids))
        assert all(re.fullmatch(r"[a-z]+[0-9a-f]{12,13}", i) for i in ids), key
    for mac in (fx.ROUTER_MAC, fx.NAS_MAC, fx.PHONE_MAC, fx.SWITCH_MAC, fx.CAM_MAC):
        assert mac.lower() not in text and mac.lower().replace(":", "-") not in text
    assert "02:00:5e" not in text


def test_no_secret_reaches_the_state_or_the_topology():
    raw = raw_of()
    raw["pppoe_clients"][0]["password"] = "pppoe-secret-pw"
    raw["routerboard"]["serial-number"] = "ABC123456"
    text = json.dumps(state(raw)) + json.dumps(model.topology_event(raw, 1.0))
    for needle in ("pppoe-secret-pw", "line-user", "ABC123456", fx.PASSWORD):
        assert needle not in text


# ---------------------------------------------------------------------------- equipment
def test_router_and_neighbours_are_equipment_with_uplinks_and_stats():
    st = state()
    names = [e["name"] for e in st["equipment"]]
    assert names == ["edge-router", "core-switch", "hall-ap"]
    r, sw, ap = st["equipment"]
    assert (r["kind"], r["model"], r["firmware"], r["mgmt_ip"], r["online"]) == ("router", "hEX S", fx.VERSION, "10.77.0.1", True)
    assert r["stats"]["cpu"] == 3 and r["stats"]["mem"] == 25.0 and r["stats"]["uptime_s"] == 7 * 86400 + 2 * 86400 + 3 * 3600 + 4 * 60 + 5
    assert r["stats"]["clients"] == sum(1 for d in st["devices"] if d["online"])
    assert (sw["kind"], sw["model"], sw["firmware"]) == ("switch", "CRS305-1G-4S+", "7.15.2")
    assert ap["kind"] == "ap" and ap["uplink"]["device"] == r["id"]
    assert sw["uplink"] == {"device": r["id"], "port": 2, "medium": None}       # seen twice (two interface lists), listed once


def test_a_switch_board_is_a_switch_and_a_foreign_neighbour_is_other():
    raw = raw_of()
    raw["resource"]["board-name"] = "CRS326-24G-2S+"
    raw["neighbors"].append({"interface": "ether3", "address4": "10.77.0.9", "mac-address": "02:00:5E:77:00:01", "identity": "printer",
                             "platform": "Something Else", "board": "X1", "version": "1.0"})
    st = state(raw)
    assert st["equipment"][0]["kind"] == "switch"
    assert by(st["equipment"], "name", "printer")["kind"] == "other"


def test_without_a_reply_for_resources_the_router_still_shows_by_name():
    raw = {"interfaces": raw_of()["interfaces"], "identity": {"name": "r1"}}
    r = state(raw)["equipment"][0]
    assert r["name"] == "r1" and r["firmware"] is None and r["stats"]["cpu"] is None


@pytest.mark.parametrize("text,seconds", [("1w2d3h4m5s", 788645), ("45s", 45), ("2h", 7200), ("3d12h", 302400), ("", None),
                                          ("soon", None), (90, 90)])
def test_uptime_parser(text, seconds):
    assert model.uptime_s(text) == seconds


def test_version_text_is_reduced_to_the_number():
    assert model.version_of({"resource": {"version": "7.16.1 (stable)"}}) == "7.16.1"
    assert model.version_of({"resource": {"version": "7.17rc2"}}) == "7.17"
    assert model.version_of({}) == ""


# ---------------------------------------------------------------------------- networks
def test_networks_come_from_addresses_on_lan_side_interfaces():
    st = state()
    lan, cams, off = st["networks"]
    assert (lan["cidr"], lan["vlan"], lan["gateway_ip"], lan["management"], lan["enabled"]) == ("10.77.0.0/24", None, "10.77.0.1", True, True)
    assert lan["dhcp"] == {"on": True, "range": ["10.77.0.100", "10.77.0.200"]}
    assert (cams["name"], cams["vlan"], cams["cidr"], cams["dhcp"]["on"]) == ("Cameras", 20, "10.77.20.0/24", False)
    assert off["enabled"] is False
    assert not any(n["cidr"].startswith(("203.0.113", "198.51.100")) for n in st["networks"])      # internet side is not a LAN


def test_a_bridge_that_filters_by_vlan_puts_its_own_address_in_its_pvid():
    raw = raw_of()
    raw["bridges"][0]["pvid"] = "10"
    assert by(state(raw)["networks"], "cidr", "10.77.0.0/24")["vlan"] == 10


def test_a_bridge_without_vlan_filtering_has_no_vlan():
    raw = raw_of()
    raw["bridges"][0]["vlan-filtering"] = "false"
    raw["bridges"][0]["pvid"] = "10"
    assert by(state(raw)["networks"], "cidr", "10.77.0.0/24")["vlan"] is None


def test_the_same_address_twice_and_broken_addresses_make_one_or_no_network():
    raw = raw_of()
    raw["addresses"] += [dict(raw["addresses"][0]), {"address": "nonsense", "interface": "bridge"}, {"interface": "bridge"}]
    assert len(state(raw)["networks"]) == 3


def test_management_is_the_network_that_holds_the_neighbours():
    raw = raw_of()
    raw["neighbors"] = [dict(n, **{"address4": "10.77.20.5"}) for n in raw["neighbors"]]
    nets = {n["name"]: n for n in state(raw)["networks"] if n["enabled"]}
    assert nets["Cameras"]["management"] and not nets["bridge"]["management"]


# ---------------------------------------------------------------------------- ports
def test_ports_are_the_ethernet_interfaces_with_link_speed_and_poe():
    st = state()
    assert [p["name"] for p in st["ports"]] == ["ether1", "to switch", "ether3", "ether4", "ether5", "sfp1"]
    assert [p["idx"] for p in st["ports"]] == [1, 2, 3, 4, 5, 6]
    p1, p2, p3, p4, p5, p6 = st["ports"]
    assert (p1["link"], p1["speed"], p1["rx_bytes"], p1["tx_bytes"]) == (True, 1000, 123456789, 987654321)
    assert p2["speed"] == 2500 and p3["speed"] == 100
    assert p4["link"] is False and p4["speed"] is None
    assert p5["disabled"] is True and p5["link"] is False
    assert p6["media"] == "sfp" and p1["media"] == "copper"
    assert p3["poe"]["capable"] and p3["poe"]["on"] and p3["poe"]["powered"]
    assert p4["poe"]["capable"] and not p4["poe"]["on"]
    assert p1["poe"] is None


def test_vlan_logic_native_untagged_and_tagged_networks():
    st = state()
    nets = {n["name"]: n["id"] for n in reversed(st["networks"])}           # the first "bridge" network is the used one
    p2, p3 = by(st["ports"], "name", "to switch"), by(st["ports"], "name", "ether3")
    assert p2["native_vlan"] is None and p2["native_network"] == nets["bridge"]
    assert p2["tagged_mode"] == "custom" and p2["tagged"] == [nets["Cameras"]] and p2["tagged_vlans"] == [20]
    assert p3["native_vlan"] == 20 and p3["native_network"] == nets["Cameras"] and p3["tagged_mode"] == "none"
    assert p2["bridge"] == "bridge"


def test_a_disabled_vlan_entry_tags_nothing_and_ranges_are_bounded():
    assert by(state()["ports"], "name", "to switch")["tagged_vlans"] == [20]            # 30-31 is disabled
    assert model.vlan_ids("10,20,30-32") == [10, 20, 30, 31, 32]
    assert model.vlan_ids("0,4095,5000,x,9-3") == []
    assert len(model.vlan_ids("1-4094,1-4094")) == 4094


def test_the_port_of_the_internet_line_is_marked_uplink_and_wan():
    st = state()
    p1 = by(st["ports"], "name", "ether1")
    assert p1["is_uplink"] and p1["wan"]
    assert not any(p["is_uplink"] for p in st["ports"] if p["name"] != "ether1")


def test_neighbours_mark_their_port_as_infrastructure():
    st = state()
    sw = by(st["equipment"], "name", "core-switch")
    p2 = by(st["ports"], "name", "to switch")
    assert p2["infra"] and p2["lldp_neighbor"] == sw["id"]
    assert by(st["ports"], "name", "ether4")["infra"]
    assert not by(st["ports"], "name", "ether3")["infra"]


# ---------------------------------------------------------------------------- end devices
def test_devices_merge_leases_address_table_host_table_and_wireless_tables():
    st = state()
    names = sorted(d["name"] for d in st["devices"])
    assert names == ["?", "?", "?", "laptop", "nas", "old-printer", "phone"]
    nas = by(st["devices"], "name", "nas")
    assert (nas["ip"], nas["fixed"], nas["fixed_ip"], nas["wired"], nas["port"], nas["online"]) == ("10.77.0.50", True, "10.77.0.50", True, "ether2", True)
    assert nas["network"] == by(st["networks"], "name", "bridge")["id"]
    phone = by(st["devices"], "name", "phone")
    assert (phone["wired"], phone["ssid"], phone["band"], phone["port"], phone["fixed"]) == (False, "Home", "5ghz", None, False)
    guest = next(d for d in st["devices"] if d["ssid"] == "Guest")
    assert guest["band"] == "2.4ghz" and guest["wired"] is False


def test_leases_alone_do_not_make_a_device_online_and_disabled_leases_are_ignored():
    st = state()
    assert by(st["devices"], "name", "old-printer")["online"] is False
    assert not any(d["name"] == "disabled-lease" for d in st["devices"])


def test_the_router_itself_multicast_addresses_and_the_internet_gateway_are_no_devices():
    st = state()
    for d in st["devices"]:
        assert d["ip"] != "203.0.113.1"
    assert len(st["devices"]) == 7                                       # router, multicast, upstream gateway, failed ARP left out


def test_alone_on_port_and_network_by_port():
    st = state()
    cam = by(st["devices"], "ip", "10.77.20.10")
    assert cam["alone_on_port"] is True and cam["network"] == by(st["networks"], "name", "Cameras")["id"]
    assert cam["network_by_port"] is True                                  # the port's own VLAN decides
    assert by(st["devices"], "name", "nas")["alone_on_port"] is False      # two hosts behind ether2
    assert by(st["devices"], "name", "nas")["network_by_port"] is False


def test_private_hardware_address_flag():
    st = state()
    assert by(st["devices"], "name", "nas")["private_address"] is True       # locally administered
    assert by(st["devices"], "name", "phone")["private_address"] is True     # 0xAA has the bit too
    raw = raw_of()
    raw["leases"][0]["mac-address"] = "00:11:22:33:44:55"
    raw["arp"][0]["mac-address"] = "00:11:22:33:44:55"
    raw["bridge_hosts"][0]["mac-address"] = "00:11:22:33:44:55"
    assert by(state(raw)["devices"], "name", "nas")["private_address"] is False


def test_static_leases_are_reservations_with_their_client():
    st = state()
    assert len(st["dhcp_reservations"]) == 1
    res = st["dhcp_reservations"][0]
    nas = by(st["devices"], "name", "nas")
    assert res["client"] == nas["id"] and res["ip"] == "10.77.0.50" and res["name"] == "nas" and res["network"] == nas["network"]


# ---------------------------------------------------------------------------- internet lines
def test_internet_lines_primary_backup_and_state():
    st = state()
    main, backup = st["wans"]
    assert (main["name"], main["type"], main["role"], main["up"], main["active"], main["link"]) == ("ether1", "dhcp", "primary", True, True, True)
    assert (backup["name"], backup["type"], backup["role"], backup["up"], backup["active"]) == ("pppoe-backup", "pppoe", "backup", False, False)
    assert main["kind"] == "cable" and main["device"] == st["equipment"][0]["id"]
    assert "iface" not in main


def test_a_standby_line_whose_client_is_connected_is_up_and_no_finding():
    raw = raw_of()
    raw["pppoe_clients"][0]["running"] = "true"
    st = state(raw)
    backup = by(st["wans"], "name", "pppoe-backup")
    assert backup["up"] is True and backup["active"] is False
    assert not [f for f in st["findings"] if f["kind"] == "wan_down"]


def test_a_dead_primary_line_is_an_alert_and_the_backup_can_take_over():
    raw = raw_of()
    raw["routes"][0]["active"] = "false"
    raw["routes"][0]["inactive"] = "true"
    raw["dhcp_clients"][0]["status"] = "searching..."
    raw["routes"][1]["active"] = "true"
    raw["routes"][1].pop("inactive")
    raw["pppoe_clients"][0]["running"] = "true"
    st = state(raw)
    assert by(st["wans"], "name", "ether1")["up"] is False and by(st["wans"], "name", "pppoe-backup")["active"] is True
    f = [f for f in st["findings"] if f["kind"] == "wan_down"]
    assert len(f) == 1 and f[0]["level"] == "alert" and f[0]["params"] == {"wan": "ether1"}


def test_a_cellular_line_is_marked_cellular_and_a_static_default_route_makes_a_line():
    raw = raw_of()
    raw["routes"].append({".id": "*53", "dst-address": "0.0.0.0/0", "gateway": "lte1", "immediate-gw": "lte1", "active": "true", "distance": "20"})
    st = state(raw)
    lte = by(st["wans"], "name", "lte1")
    assert lte["kind"] == "cellular" and lte["type"] == "static" and lte["role"] == "backup" and lte["up"] is True


def test_disabled_default_route_and_disabled_client_make_no_alert():
    raw = raw_of()
    raw["dhcp_clients"][0]["disabled"] = "true"
    raw["routes"][0]["disabled"] = "true"
    st = state(raw)
    assert by(st["wans"], "name", "ether1")["enabled"] is False
    assert not any(f["kind"] == "wan_down" and f["params"]["wan"] == "ether1" for f in st["findings"])


# ---------------------------------------------------------------------------- rules and forwards
def test_nat_destination_rules_become_port_forwards():
    st = state()
    assert [f["name"] for f in st["port_forwards"]] == ["nas web", "#3"]
    web, friends = st["port_forwards"]
    assert (web["proto"], web["wan_port"], web["dst_ip"], web["dst_port"], web["enabled"], web["source_limited"]) == ("tcp", "8443", "10.77.0.50", "443", True, False)
    assert (friends["proto"], friends["enabled"], friends["source_limited"], friends["dst_port"]) == ("udp", False, True, "51000")
    assert not any(f["dst_ip"] is None for f in st["port_forwards"])        # masquerade and redirect are no forwards


def test_filter_rules_are_shown_never_written_with_a_digest_for_the_night_report():
    st = state()
    assert [r["title"] for r in st["raw"]] == ["established", "drop from wan", "temporary open", "#4 forward drop"]
    assert all(r["editable"] is False and r["area"] == "acl" for r in st["raw"])
    assert [r["enabled"] for r in st["raw"]] == [True, True, False, True]
    assert [r["action"] for r in st["raw"]] == ["allow", "block", "allow", "block"]
    dig = {r["name"]: r for r in st["rule_digest"]}
    assert dig["drop from wan"]["scope"] == "input" and dig["drop from wan"]["allow"] is False
    assert [r["order"] for r in st["raw"]] == [0, 1, 2, 3]


def test_the_digest_fingerprint_follows_what_decides_the_rule_not_the_comment():
    base = raw_of()
    changed = raw_of()
    changed["filter"][1]["in-interface"] = "ether5"
    renamed = raw_of()
    renamed["filter"][1]["comment"] = "another name"
    sig = lambda raw: state(raw)["rule_digest"][1]["sig"]
    assert sig(base) != sig(changed) and sig(base) == sig(renamed)


# ---------------------------------------------------------------------------- checks
def test_findings_wan_down_for_the_backup_and_rules_switched_off():
    st = state()
    kinds = [(f["kind"], f["level"]) for f in st["findings"]]
    assert kinds == [("wan_down", "alert"), ("rules_disabled", "info")]
    assert by(st["findings"], "kind", "rules_disabled")["params"] == {"n": 1}


def test_input_open_when_no_enabled_drop_rule_guards_the_input_chain():
    raw = raw_of()
    raw["filter"][1]["disabled"] = "true"
    f = by(state(raw)["findings"], "kind", "input_open")
    assert f["level"] == "warn"
    raw = raw_of()
    raw["filter"][1]["action"] = "reject"
    assert not [f for f in state(raw)["findings"] if f["kind"] == "input_open"]


def test_input_open_is_not_claimed_when_the_rules_were_not_read_or_there_is_no_internet_line():
    raw = raw_of()
    del raw["filter"]
    assert not [f for f in state(raw)["findings"] if f["kind"] == "input_open"]
    raw = raw_of()
    raw["filter"] = []
    assert [f for f in state(raw)["findings"] if f["kind"] == "input_open"]
    for k in ("routes", "dhcp_clients", "pppoe_clients"):
        raw[k] = []
    assert not [f for f in state(raw)["findings"] if f["kind"] == "input_open"]


def test_findings_are_sorted_most_important_first_with_unique_ids():
    raw = raw_of()
    raw["filter"][1]["disabled"] = "true"
    fs = state(raw)["findings"]
    assert [f["level"] for f in fs] == sorted((f["level"] for f in fs), key=lambda l: {"alert": 0, "warn": 1, "info": 2}[l])
    assert len({f["id"] for f in fs}) == len(fs)


# ---------------------------------------------------------------------------- topology observation
def test_topology_event_has_the_router_the_neighbours_and_the_clients():
    t = model.topology_event(raw_of(), 123.0)
    assert t["type"] == "topology" and t["source"] == "controller" and t["t"] == 123.0
    assert [d["name"] for d in t["devices"]] == ["edge-router", "core-switch", "hall-ap"]
    router, sw, ap = t["devices"]
    assert router["mac"] == fx.ROUTER_MAC.lower() and router["role"] == "router" and router["uplink"] is None
    assert sw["uplink"] == {"mac": router["mac"], "port": "ether2", "own_port": None, "medium": None}
    assert ap["role"] == "ap"
    macs = {c["mac"]: c for c in t["clients"]}
    assert fx.NAS_MAC.lower() in macs and macs[fx.NAS_MAC.lower()]["port"] == "ether2" and macs[fx.NAS_MAC.lower()]["name"] == "nas"
    assert macs[fx.PHONE_MAC.lower()]["medium"] == "radio" and macs[fx.PHONE_MAC.lower()]["ssid"] == "Home" and macs[fx.PHONE_MAC.lower()]["band"] == "5ghz"
    assert macs[fx.TABLET_MAC.lower()]["band"] == "2.4ghz"
    for c in t["clients"]:
        assert c["parent_mac"] == router["mac"]
    assert fx.ROUTER_MAC.lower() not in macs and fx.MULTICAST_MAC.lower() not in macs


def test_topology_event_without_a_router_address_has_no_clients():
    t = model.topology_event({"interfaces": [], "bridge_hosts": raw_of()["bridge_hosts"]}, 1.0)
    assert t["devices"] == [] and t["clients"] == []
    assert model.topology_event(None, 1.0)["clients"] == []


def test_signal_is_read_from_both_registration_tables_and_cleaned():
    t = model.topology_event(raw_of(), 1.0)
    macs = {c["mac"]: c for c in t["clients"]}
    assert macs[fx.PHONE_MAC.lower()]["signal"] == -55 and macs[fx.TABLET_MAC.lower()]["signal"] == -60
    raw = raw_of()
    raw["wifi_reg"][0].pop("signal"); raw["wifi_reg"][0]["signal-strength"] = "-71@5GHz"        # the older package names it differently
    raw["wireless_reg"][0]["signal"] = "12"                                                      # not a dBm reading
    macs = {c["mac"]: c for c in model.topology_event(raw, 1.0)["clients"]}
    assert macs[fx.PHONE_MAC.lower()]["signal"] == -71 and macs[fx.TABLET_MAC.lower()]["signal"] is None
