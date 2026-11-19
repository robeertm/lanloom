"""Translation of the UniFi Network family's replies into neutral model v2 and into topology observations.
Pure functions, no network."""
import json

import pytest

from lanloom import topology
from lanloom.firewall import neutral
from lanloom.integrations.unifi import model
from lanloom.sources import _ctlmap

from . import fixtures as fx


def by_name(items, name):
    return next(i for i in items if i.get("name") == name or i.get("title") == name)


def ids_of(state, key):
    return {i["id"] for i in state[key]}


def all_ids(state):
    out = []
    for key in ("zones", "networks", "devices", "equipment", "ports", "policies", "ssids", "dhcp_reservations",
                "port_forwards", "wans", "vpn", "raw"):
        out += [i["id"] for i in state[key]]
    return out


# ---------------------------------------------------------------------------- shape and ids
def test_every_combination_is_a_valid_neutral_state():
    for raw in (fx.classic(), fx.official(), fx.both(), {}, {"info": None}):
        st = model.to_neutral(raw)
        neutral.validate(st)
        assert st["v"] == 2


def test_ids_are_opaque_and_stable():
    st = model.to_neutral(fx.both())
    macs = (fx.MAC_GW, fx.MAC_SW, fx.MAC_AP, fx.MAC_OFFICE, fx.MAC_PHONE)
    ids = all_ids(st)
    for key in ("zones", "networks", "devices", "equipment", "ports", "policies", "ssids", "port_forwards", "raw"):
        assert len(ids_of(st, key)) == len(st[key]), key          # wans and vpn share the id of their network on purpose
    for i in ids:
        assert not any(m.replace(":", "") in i or m in i for m in macs)
        assert len(i) <= 14 and i[0] in "znedprsfxcv" and ":" not in i
    assert model.to_neutral(fx.both()) == st
    assert model.oid("c", fx.MAC_OFFICE) == model.oid("c", fx.MAC_OFFICE) != model.oid("c", fx.MAC_PHONE)


def test_classic_objects_with_external_id_give_the_official_neutral_ids():
    c, o = model.to_neutral(fx.classic()), model.to_neutral(fx.official())
    for key in ("zones", "networks"):
        assert ids_of(o, key) <= ids_of(c, key), key
    assert ids_of(o, "equipment") == ids_of(c, "equipment")
    assert ids_of(o, "policies") - {model.oid("r", fx.O_POL_SSH)} <= ids_of(c, "policies")      # the SSH rule is official-only
    assert {s["id"] for s in o["ssids"]} == {s["id"] for s in c["ssids"]}
    # references resolve to the same id as well
    assert by_name(c["networks"], "Guest")["zone"] == by_name(o["networks"], "Guest")["zone"] == by_name(o["zones"], "Guest")["id"]
    assert by_name(c["policies"], "Block guest to internal")["src"] == by_name(o["policies"], "Block guest to internal")["src"]


def test_both_apis_do_not_double_objects():
    st = model.to_neutral(fx.both())
    assert len(st["zones"]) == 3 and len(st["wans"]) == 1
    assert sorted(n["name"] for n in st["networks"]) == ["Default", "Guest", "Lab", "VPN server", "WAN 1"]
    assert len(st["equipment"]) == 3 and len(st["ssids"]) == 1
    assert st["controller"] == {"version": "11.0.1", "apis": ["classic", "official"]}


# ---------------------------------------------------------------------------- official wins, classic fills
def test_official_wins_and_classic_fills_gaps():
    st = model.to_neutral(fx.both())
    pols = {p["name"]: p for p in st["policies"]}
    assert len(pols) == 6
    assert pols["Allow internal to external"]["hits"] == 4242            # counter only the classic API has
    assert pols["Allow internal to external"]["last_hit"] == 1790000000
    assert pols["Allow SSH to office PC"]["hits"] is None                # official-only policy: no counter
    assert pols["Allow SSH to office PC"]["ports"] == "22" and pols["Allow SSH to office PC"]["proto"] == "tcp"
    assert pols["Allow internal to external"]["builtin"] is True         # the official metadata decides
    assert st["controller"]["version"] == "11.0.1"                       # official info before classic sysinfo
    lab = by_name(st["networks"], "Lab")                                 # known to the classic API only
    assert lab["cidr"] == "203.0.113.0/24" and lab["vlan"] == 20
    assert by_name(st["networks"], "Guest")["isolated"] is True


def test_ports_forwards_reservations_and_client_ports_come_from_classic():
    st = model.to_neutral(fx.both())
    assert len(st["ports"]) == 4
    p2 = next(p for p in st["ports"] if p["idx"] == 2)
    assert {k: p2["poe"][k] for k in ("capable", "on", "watts")} == {"capable": True, "on": True, "watts": 3.5} and p2["profile_name"] == "Office ports" and p2["speed"] == 1000
    uplink = next(p for p in st["ports"] if p["idx"] == 8)
    assert uplink["is_uplink"] and uplink["infra"] and uplink["lldp_neighbor"] == model.oid("e", fx.MAC_GW)
    assert [f["name"] for f in st["port_forwards"]] == ["Web"] and st["port_forwards"][0]["dst_ip"] == "192.168.50.20"
    assert {r["ip"] for r in st["dhcp_reservations"]} == {"192.168.50.20", "192.168.50.40"}
    office = by_name(st["devices"], "Office PC")
    assert office["fixed"] and office["wired"] and office["alone_on_port"] is True
    assert office["parent"] == by_name(st["equipment"], "Switch hall")["id"]
    lab1, lab2 = by_name(st["devices"], "Lab PC"), by_name(st["devices"], "Lab NAS")
    assert not lab1["alone_on_port"] and not lab2["alone_on_port"]       # two clients behind one switch port
    phone = by_name(st["devices"], "Phone")
    assert phone["band"] == "5ghz" and phone["parent"] == by_name(st["equipment"], "AP hall")["id"]
    assert phone["private_address"] is True and by_name(st["devices"], "Guest phone")["guest"] is True


def test_official_only_has_the_basics_but_no_classic_extras():
    st = model.to_neutral(fx.official())
    assert st["controller"]["apis"] == ["official"]
    assert [p["idx"] for p in st["ports"]] == [2, 3, 4]                  # port list from the device details
    assert st["port_forwards"] == [] and st["dhcp_reservations"] == []
    assert {d["name"] for d in st["devices"]} == {"Office PC", "Phone", "Guest phone"}      # VPN clients are not end devices
    assert by_name(st["devices"], "Guest phone")["guest"] is True
    assert {e["kind"] for e in st["equipment"]} == {"router", "switch", "ap"}
    sw = by_name(st["equipment"], "Switch hall")
    assert sw["uplink"]["device"] == by_name(st["equipment"], "Gateway")["id"] and sw["online"] is True
    assert {r["area"] for r in st["raw"]} == {"acl", "dns_policy"} and all(r["editable"] is False for r in st["raw"])
    ssid = st["ssids"][0]
    assert ssid["security"] == "wpa2_personal" and ssid["bands"] == ["2.4ghz", "5ghz"] and ssid["network"] == by_name(st["networks"], "Default")["id"]


def test_classic_only_has_hits_ports_and_no_acl():
    st = model.to_neutral(fx.classic())
    assert st["controller"] == {"version": "10.2.0", "apis": ["classic"]}
    assert by_name(st["policies"], "Allow guest to external")["hits"] == 99
    assert all(p["editable"] is False for p in st["policies"])
    assert "acl" not in {r["area"] for r in st["raw"]}
    dhcp = by_name(st["networks"], "Default")["dhcp"]
    assert dhcp == {"on": True, "range": ["192.168.50.100", "192.168.50.199"]}
    assert by_name(st["networks"], "Lab")["dhcp"] == {"on": False, "range": None}


# ---------------------------------------------------------------------------- management, zones, policies
def test_management_network_is_the_one_with_the_equipment():
    for raw in (fx.classic(), fx.official(), fx.both()):
        st = model.to_neutral(raw)
        marked = [n["name"] for n in st["networks"] if n["management"]]
        assert marked == ["Default"]
    raw = fx.classic()                                                   # move the equipment: the marking follows
    for d in raw["device"]["data"]:
        d["ip"] = d["ip"].replace("192.168.50.", "203.0.113.")
    assert [n["name"] for n in model.to_neutral(raw)["networks"] if n["management"]] == ["Lab"]
    assert not any(n["management"] for n in model.to_neutral(fx.only(fx.classic(), "networkconf"))["networks"])


def test_zone_internet_is_derived_from_the_policies():
    for raw in (fx.classic(), fx.official(), fx.both()):
        st = model.to_neutral(raw)
        z = {x["name"]: x for x in st["zones"]}
        assert z["Internal"]["internet"] and z["Guest"]["internet"] and not z["External"]["internet"]
        assert z["External"]["system"] == "external" and z["Internal"]["system"] == "internal" and z["Guest"]["system"] is None
        assert z["Guest"]["builtin"] is False and z["Internal"]["builtin"] is True
        pairs = {(a, b) for a, b in st["allow"]}
        assert (z["Guest"]["id"], z["Internal"]["id"]) not in pairs
        assert (z["Internal"]["id"], z["External"]["id"]) in pairs
    st = model.to_neutral(fx.only(fx.classic(), "zone", "networkconf"))      # zones without any policy: nothing derived
    assert st["allow"] == [] and not any(z["internet"] for z in st["zones"])


def test_state_filtered_or_scheduled_policies_do_not_decide_a_zone_pair():
    for raw in (fx.classic(), fx.official()):
        st = model.to_neutral(raw)
        z = {x["name"]: x["id"] for x in st["zones"]}
        pols = {p["name"]: p for p in st["policies"]}
        assert pols["Drop invalid"]["states"] == ["invalid"] and pols["Evening block"]["scheduled"] is True
        assert pols["Allow guest to external"]["scheduled"] is False and pols["Allow guest to external"]["states"] == []
        pairs = {(a, b) for a, b in st["allow"]}
        # the block "Drop invalid" (internal -> guest, order 5) must not turn the whole pair into "blocked", and
        # the scheduled block (guest -> external, order 6) must not hide the allow rule behind it
        assert (z["Guest"], z["External"]) in pairs
        assert (z["Internal"], z["Guest"]) not in pairs                  # default is block anyway; no rule allows it
    raw = fx.official()                                                  # an allow filtered by state decides nothing either
    raw["o_policies"][0]["connectionStateFilter"] = ["ESTABLISHED"]
    st = model.to_neutral(raw)
    z = {x["name"]: x["id"] for x in st["zones"]}
    assert (z["Internal"], z["External"]) not in {(a, b) for a, b in st["allow"]}


def test_narrowed_policy_keeps_its_details_but_does_not_open_a_zone_pair():
    st = model.to_neutral(fx.official())
    ssh = by_name(st["policies"], "Allow SSH to office PC")
    assert ssh["src"]["match"]["kind"] == "network" and ssh["src"]["match"]["items"] == [by_name(st["networks"], "Guest")["id"]]
    z = {x["name"]: x["id"] for x in st["zones"]}
    assert (z["Guest"], z["Internal"]) not in {(a, b) for a, b in st["allow"]}


def test_legacy_firewall_rules_are_shown_as_raw_and_never_editable():
    st = model.to_neutral(fx.classic())
    legacy = [r for r in st["raw"] if r["area"] == "legacy_rule"]
    assert len(legacy) == 1 and legacy[0]["editable"] is False and legacy[0]["title"] == "Old rule"
    assert legacy[0]["ruleset"] == "LAN_IN" and legacy[0]["action"] == "drop"
    assert not st["policies"] or all(p["name"] != "Old rule" for p in st["policies"])
    only_legacy = model.to_neutral(fx.only(fx.classic(), "firewallrule"))
    assert only_legacy["policies"] == [] and [r["area"] for r in only_legacy["raw"]] == ["legacy_rule"]
    neutral.validate(only_legacy)


def test_wans_and_vpn_lists():
    for raw in (fx.classic(), fx.official(), fx.both()):
        st = model.to_neutral(raw)
        assert [w["name"] for w in st["wans"]] == ["WAN 1"]
        assert st["wans"][0]["id"] == model.oid("n", fx.O_NET_WAN)
        assert st["vpn"]
    c = model.to_neutral(fx.classic())
    assert c["wans"][0]["type"] == "dhcp" and c["vpn"][0]["kind"] == "remote-user-vpn" and c["vpn"][0]["type"] == "tunnel-server"
    o = model.to_neutral(fx.official())
    assert o["vpn"][0]["name"] == "Remote access" and o["vpn"][0]["enabled"] is True


# ---------------------------------------------------------------------------- secrets
def test_passphrases_and_keys_never_reach_the_output():
    for raw in (fx.classic(), fx.official(), fx.both()):
        dump = json.dumps(model.to_neutral(raw))
        for secret in (fx.PASS, fx.PSK, "passphrase", "x_passphrase", "preshared"):
            assert secret not in dump, secret
    sent = json.dumps(model.topology_event(fx.both(), 1.0))
    assert fx.PASS not in sent and fx.PSK not in sent


def test_hostile_input_is_tolerated():
    raw = {"networkconf": {"data": [None, 5, "x", {"_id": "a", "ip_subnet": "not-an-ip"}]}, "o_policies": [{"id": 1, "source": 3}],
           "sta": {"data": [{"mac": 5}, {"mac": "zz"}]}, "device": "nope", "o_devices": [{"id": "d", "features": 7, "interfaces": []}]}
    neutral.validate(model.to_neutral(raw))
    assert model.to_neutral(None)["v"] == 2
    assert model.text("a\x00b\n" + "x" * 500, 10) == "ab" + "x" * 8
    assert model.num(True) is None and model.num("5") == 5 and model.num("1.5") == 1.5 and model.num([1]) is None
    assert model.items({"data": [{"a": 1}, 3]}, "data") == [{"a": 1}] and model.items({"k": {"data": [{"a": 1}, 3]}}, "k") == [{"a": 1}]


# ---------------------------------------------------------------------------- topology observation
def test_classic_topology_equals_the_generic_controller_mapping():
    raw = fx.classic()
    ev = model.topology_event(raw, 5.0)
    assert ev["type"] == "topology" and ev["source"] == "controller" and ev["t"] == 5.0
    devices = _ctlmap._unique([_ctlmap.map_device(d) for d in raw["device"]["data"]], 1000)
    clients = _ctlmap._unique([_ctlmap.map_client(c) for c in raw["sta"]["data"]], 5000)
    assert ev["devices"] == devices and ev["clients"] == clients
    assert len(ev["devices"]) == 3 and len(ev["clients"]) == 5
    sw = next(d for d in ev["devices"] if d["mac"] == fx.MAC_SW)
    assert sw["role"] == "switch" and sw["uplink"]["mac"] == fx.MAC_GW and sw["uplink"]["port"] == "1"
    office = next(c for c in ev["clients"] if c["mac"] == fx.MAC_OFFICE)
    assert office["parent_mac"] == fx.MAC_SW and office["port"] == "2" and office["medium"] == "wired"
    phone = next(c for c in ev["clients"] if c["mac"] == fx.MAC_PHONE)
    assert phone["parent_mac"] == fx.MAC_AP and phone["band"] == "5ghz" and phone["ssid"] == "Home"


def test_official_only_topology_has_devices_and_clients_with_parent_macs():
    ev = model.topology_event(fx.official(), 7.0)
    assert ev["source"] == "controller"
    roles = {d["mac"]: d["role"] for d in ev["devices"]}
    assert roles == {fx.MAC_GW: "router", fx.MAC_SW: "switch", fx.MAC_AP: "ap"}
    ups = {d["mac"]: (d["uplink"] or {}).get("mac") for d in ev["devices"]}
    assert ups == {fx.MAC_GW: None, fx.MAC_SW: fx.MAC_GW, fx.MAC_AP: fx.MAC_SW}
    clients = {c["mac"]: c for c in ev["clients"]}
    assert set(clients) == {fx.MAC_OFFICE, fx.MAC_PHONE, fx.MAC_GUEST}      # the VPN client has no uplink device
    assert clients[fx.MAC_OFFICE]["parent_mac"] == fx.MAC_SW and clients[fx.MAC_OFFICE]["medium"] == "wired"
    assert clients[fx.MAC_PHONE]["parent_mac"] == fx.MAC_AP and clients[fx.MAC_PHONE]["medium"] == "radio"


def test_classic_wins_over_official_for_the_topology_observation():
    raw = fx.both()
    assert model.topology_event(raw, 1.0)["clients"] == model.topology_event(fx.classic(), 1.0)["clients"]
    assert model.topology_event({}, 1.0) == {"type": "topology", "source": "controller", "t": 1.0, "devices": [], "clients": []}


def _inventory(*rows):
    out = []
    for i, (did, mac, ip, kind) in enumerate(rows):
        out.append({"id": did, "ip": ip, "mac": mac, "kind": kind, "title": did, "hostnames": [], "is_gateway": kind == "router"})
    return out


def test_resolver_hangs_a_client_under_its_switch_with_source_controller():
    inventory = _inventory(("gw", fx.MAC_GW, "192.168.50.1", "router"), ("office", fx.MAC_OFFICE, "192.168.50.20", "pc"),
                           ("phone", fx.MAC_PHONE, "192.168.50.30", "phone"))
    for raw in (fx.classic(), fx.official()):
        obs = {"int:1": {"integ": 1, "ev": model.topology_event(raw, 100.0)}}
        topo = topology.resolve(inventory, {}, obs, now=110.0)
        link = topo["links"]["office"]
        parent = topo["nodes"][link["parent"]]
        assert link["source"] == "controller" and link["medium"] in ("wired", None)
        assert parent["mac"] == fx.MAC_SW and parent["kind"] == "switch" and parent["title"] == "Switch hall"
        assert topo["links"]["phone"]["source"] == "controller"
        assert topo["nodes"][topo["links"]["phone"]["parent"]]["mac"] == fx.MAC_AP
        sw_link = topo["links"][link["parent"]]
        assert sw_link["parent"] == "gw" and sw_link["source"] == "controller"      # the switch sits behind the gateway
    classic_topo = topology.resolve(inventory, {}, {"k": {"integ": 1, "ev": model.topology_event(fx.classic(), 100.0)}}, now=110.0)
    assert classic_topo["links"]["office"]["port"] == "2"


# ---------------------------------------------------------------------------------------------------- internet lines (control room)
MOBILE_NET = "5f0000000000000000000099"


def _wan_raw(failover="failover-only", main_prio=1, mobile_prio=2, wan1=None, wan2=None, gateway_type="ugw", uplink=None):
    raw = fx.classic()
    nets = raw["networkconf"]["data"]
    main = next(n for n in nets if n["_id"] == fx.C_NET_WAN)
    main.update({"wan_load_balance_type": failover, "wan_failover_priority": main_prio, "attr_hidden_id": "WAN"})
    nets.append({"_id": MOBILE_NET, "name": "Mobile backup", "purpose": "wan", "wan_type": "dhcp", "enabled": True, "wan_load_balance_type": failover,
                 "wan_failover_priority": mobile_prio, "attr_hidden_id": "WAN2"})
    gw = next(d for d in raw["device"]["data"] if d["mac"] == fx.MAC_GW)
    gw["type"] = gateway_type
    if wan1 is not None:
        gw["wan1"] = wan1
    if wan2 is not None:
        gw["wan2"] = wan2
    if uplink is not None:
        gw["uplink"] = uplink
    return raw


def _wans_of(raw):
    st = model.to_neutral(raw)
    neutral.validate(st)
    return {w["name"]: w for w in st["wans"]}


def test_wan_states_from_the_gateway_device_record():
    wans = _wans_of(_wan_raw(wan1={"up": True, "ifname": "eth4", "is_uplink": True, "rx_bytes-r": 125000, "tx_bytes-r": 25000, "latency": 9},
                             wan2={"up": False, "ifname": "eth5", "is_uplink": False, "rx_bytes-r": 0, "tx_bytes-r": 0}))
    a, b = wans["WAN 1"], wans["Mobile backup"]
    assert a["up"] is True and a["active"] is True and a["role"] == "primary" and a["kind"] == "cable"
    assert a["rx_bps"] == 1_000_000.0 and a["tx_bps"] == 200_000.0 and a["latency_ms"] == 9 and a["failover"] == "failover-only"
    assert b["up"] is False and b["active"] is False and b["role"] == "backup" and b["kind"] == "cellular" and b["rx_bps"] == 0.0
    assert "latency_ms" not in b


def test_the_carrying_line_comes_from_the_gateway_uplink_when_no_line_says_so():
    wans = _wans_of(_wan_raw(wan1={"up": False, "ifname": "eth4"}, wan2={"up": True, "ifname": "eth5"}, uplink={"name": "eth5", "type": "wire"}))
    assert wans["WAN 1"]["active"] is False and wans["Mobile backup"]["active"] is True
    wans = _wans_of(_wan_raw(wan1={"up": True, "ifname": "eth4"}, wan2={"up": True, "ifname": "eth5"}))
    assert "active" not in wans["WAN 1"] and "active" not in wans["Mobile backup"]               # nobody tells: nothing is claimed
    wans = _wans_of(_wan_raw(wan1={"up": True, "ifname": "eth4", "is_uplink": False}, wan2={"up": True, "ifname": "eth5", "is_uplink": True}, uplink={"name": "eth4"}))
    assert wans["WAN 1"]["active"] is False and wans["Mobile backup"]["active"] is True            # the line's own flag wins over the uplink name


def test_roles_follow_the_failover_priority_not_the_order():
    wans = _wans_of(_wan_raw(main_prio=2, mobile_prio=1, wan1={"up": True}, wan2={"up": True}))
    assert wans["WAN 1"]["role"] == "backup" and wans["Mobile backup"]["role"] == "primary"
    wans = _wans_of(_wan_raw(main_prio=None, mobile_prio=None, wan1={"up": True}, wan2={"up": True}))
    assert wans["WAN 1"]["role"] == "primary" and wans["Mobile backup"]["role"] == "backup"        # no priority: the order decides
    wans = _wans_of(_wan_raw(failover="weighted", wan1={"up": True}, wan2={"up": True}))
    assert wans["WAN 1"]["role"] == "primary" and wans["Mobile backup"]["role"] == "primary"       # load balancing has no reserve line


def test_a_line_without_its_own_record_keeps_no_state_fields():
    wans = _wans_of(_wan_raw(wan1={"up": True, "rx_bytes-r": 10, "ifname": "eth4"}))             # no wan2 record
    b = wans["Mobile backup"]
    for field in ("up", "active", "rx_bps", "tx_bps", "latency_ms", "kind"):
        assert field not in b, field
    assert wans["WAN 1"]["up"] is True
    plain = model.to_neutral(fx.classic())["wans"][0]                                            # the stock fixture: one line, no record
    for field in ("up", "active", "rx_bps", "tx_bps", "latency_ms", "kind", "role"):
        assert field not in plain, field
    assert plain["name"] == "WAN 1"


def test_without_a_gateway_device_there_are_no_state_fields_at_all():
    raw = _wan_raw(wan1={"up": True})
    raw["device"]["data"] = [d for d in raw["device"]["data"] if d["type"] != "ugw"]
    for w in model.to_neutral(raw)["wans"]:
        assert not ({"up", "active", "rx_bps", "tx_bps", "kind"} & set(w))


@pytest.mark.parametrize("gateway_type", ["ugw", "udm", "uxg"])
def test_every_gateway_kind_is_read(gateway_type):
    assert _wans_of(_wan_raw(gateway_type=gateway_type, wan1={"up": True}))["WAN 1"]["up"] is True


def test_a_switch_or_access_point_is_not_the_gateway():
    raw = _wan_raw(gateway_type="usw", wan1={"up": True})
    assert "up" not in _wans_of(raw)["WAN 1"]


def test_wan_record_values_that_make_no_sense_are_dropped():
    wans = _wans_of(_wan_raw(wan1={"up": "yes", "is_uplink": "true", "rx_bytes-r": "fast", "tx_bytes-r": -5, "latency": float("inf"), "ifname": "x" * 80},
                             wan2={"up": 1, "rx_bytes-r": "nan", "tx_bytes-r": "1e30", "latency": -1}))
    for w in wans.values():
        for field in ("up", "active", "rx_bps", "tx_bps", "latency_ms"):
            assert field not in w, (w["name"], field)
    assert wans["WAN 1"]["kind"] == "cable"                                                      # the record exists, the kind follows the name


def test_wan_record_that_is_no_object_is_ignored():
    wans = _wans_of(_wan_raw(wan1="up", wan2=["x"]))
    assert "up" not in wans["WAN 1"] and "up" not in wans["Mobile backup"]


def test_the_mobile_kind_is_recognised_by_name_or_type():
    raw = _wan_raw(wan1={"up": True}, wan2={"up": True, "type": "lte"})
    next(n for n in raw["networkconf"]["data"] if n["_id"] == MOBILE_NET)["name"] = "Second line"
    assert _wans_of(raw)["Second line"]["kind"] == "cellular"
    raw = _wan_raw(wan1={"up": True}, wan2={"up": True})
    next(n for n in raw["networkconf"]["data"] if n["_id"] == MOBILE_NET)["name"] = "Second line"
    assert _wans_of(raw)["Second line"]["kind"] == "cable"


def test_wan_states_do_not_leak_hardware_addresses_or_change_the_ids():
    before = {w["name"]: w["id"] for w in model.to_neutral(_wan_raw())["wans"]}
    after = {w["name"]: w["id"] for w in model.to_neutral(_wan_raw(wan1={"up": True, "ifname": "eth4", "mac": fx.MAC_GW}, wan2={"up": True}))["wans"]}
    assert before == after
    assert fx.MAC_GW not in json.dumps(model.to_neutral(_wan_raw(wan1={"up": True, "mac": fx.MAC_GW}))["wans"])
