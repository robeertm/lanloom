"""Translation of the UniFi Network family's replies into neutral model v2 and into topology observations.
Pure functions, no network."""
import json

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
    assert p2["poe"] == {"capable": True, "on": True, "watts": 3.5} and p2["profile"] == "Office ports" and p2["speed"] == 1000
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
