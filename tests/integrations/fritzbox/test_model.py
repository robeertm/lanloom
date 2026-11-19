"""The translation module: parsers, the mesh tree (whatever order the links come in), the neutral state, the topology
observation, what is never kept."""
import json

import pytest

from lanloom.firewall import neutral
from lanloom.integrations.fritzbox import model

from . import fixtures as fx


def raw_of(**kw) -> dict:
    mesh = kw.pop("mesh", fx.mesh_doc())
    hosts = kw.pop("hosts", None)
    raw = {"info": {"model": "FRITZ!Box 7590", "firmware": fx.FIRMWARE, "uptime_s": 864000},
           "lan": {"ip": fx.BOX_IP, "mask": "255.255.255.0", "dhcp": True, "first": "192.168.178.20", "last": "192.168.178.200"},
           "lan_mac": fx.BOX, "wans": [{"kind": "ip", "status": "Connected"}, {"kind": "ppp", "status": "Unconfigured"}],
           "hosts": model.parse_hostlist(fx.hostlist_xml(hosts) if hosts is not None else fx.hostlist_xml())}
    if mesh is not None:
        raw["mesh"] = mesh
    raw.update(kw)
    return raw


def by_name(state, area="devices"):
    return {o["name"]: o for o in state[area]}


# ---------------------------------------------------------------------------- parsers
def test_soap_answer_is_read_into_its_out_arguments():
    out = model.parse_soap(fx.envelope("urn:x:service:S:1", "GetInfo", {"NewA": "1", "NewB": "a & b"}))
    assert out == {"NewA": "1", "NewB": "a & b"}


def test_a_fault_gives_the_upnp_error_code():
    assert model.parse_soap(fx.fault(606)) == {"_fault": 606}
    assert model.parse_soap(fx.fault(401)) == {"_fault": 401}


@pytest.mark.parametrize("doc", [b"", b"not xml", b"<a/>", b"<s:Envelope xmlns:s='x'/>", b"\xff\xfe", None, 5,
                                 b'<!DOCTYPE x [<!ENTITY a "aaaa">]><s:Envelope xmlns:s="x"><s:Body><a>&a;</a></s:Body></s:Envelope>',
                                 b'<?xml version="1.0"?><?pi x?><s:Envelope xmlns:s="x"><s:Body><a/></s:Body></s:Envelope>'])
def test_anything_else_is_not_a_soap_answer(doc):
    assert model.parse_soap(doc) is None


def test_a_document_with_a_declaration_is_refused_whole():
    bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]><List><Item><HostName>&b;</HostName></Item></List>'
    assert model.parse_hostlist(bomb) is None
    assert model.parse_soap(bomb) is None


def test_host_list_rows():
    rows = model.parse_hostlist(fx.hostlist_xml())
    assert len(rows) == 6
    nas = next(r for r in rows if r["mac"] == fx.NAS)
    assert nas == {"ip": "192.168.178.20", "mac": fx.NAS, "active": True, "name": "NAS", "interface": "Ethernet", "port": 2,
                   "speed": 0, "guest": False, "model": ""}
    assert next(r for r in rows if r["mac"] == fx.OLD)["active"] is False
    assert model.parse_hostlist(b"<Other/>") is None


# ---------------------------------------------------------------------------- mesh
def test_the_mesh_is_oriented_from_the_box_whatever_order_the_links_come_in():
    for swapped in (False, True):
        g = model.mesh_graph(fx.mesh_doc(order_swapped=swapped))
        assert g["root"] == "n-box" and g["schema"] == "1.9"
        names = {u: n["name"] for u, n in g["nodes"].items()}
        par = {names[u]: names[p["node"]] for u, p in g["parent"].items()}
        assert par == {"Repeater upstairs": "fritz.box", "NAS": "fritz.box", "Office printer": "fritz.box",
                       "Kitchen tablet": "fritz.box", "Phone": "Repeater upstairs"}, swapped
        nas = g["parent"]["n-nas"]
        assert nas["parent_if"] == "LAN:2" and nas["own_if"] == "LAN:1" and nas["type"] == "LAN"


def test_a_link_that_is_not_connected_does_not_hang_anything():
    g = model.mesh_graph(fx.mesh_doc(repeater_up=False))
    assert "n-rep" not in g["parent"] and "n-phone" not in g["parent"]


@pytest.mark.parametrize("mesh", [None, {}, {"nodes": "x"}, {"nodes": [1, None]}, {"nodes": [{"uid": "a"}]}, []])
def test_a_missing_or_broken_mesh_is_an_empty_graph(mesh):
    g = model.mesh_graph(mesh)
    assert g["nodes"] is not None and g["root"] is None and g["parent"] == {}


def test_interface_names():
    assert model._iface_port("LAN:3") == 3 and model._iface_port("AP:5G:0") is None and model._iface_port(None) is None
    assert model._iface_band("AP:5G:0") == "5ghz" and model._iface_band("STA:2G:0") == "2.4ghz" and model._iface_band("AP:6G:1") == "6ghz"
    assert model._iface_band("LAN:1") is None


# ---------------------------------------------------------------------------- neutral state
def test_state_with_the_mesh():
    st = model.to_neutral(raw_of())
    neutral.validate(st)
    eq = by_name(st, "equipment")
    assert set(eq) == {"FRITZ!Box 7590", "Repeater upstairs"}
    box, rep = eq["FRITZ!Box 7590"], eq["Repeater upstairs"]
    assert box["kind"] == "router" and box["firmware"] == "7.57" and box["mgmt_ip"] == fx.BOX_IP and box["uplink"] is None
    assert rep["kind"] == "ap" and rep["model"] == "FRITZ!Repeater 6000" and rep["online"] is True
    assert rep["uplink"] == {"device": box["id"], "port": None, "medium": "radio"}
    dev = by_name(st)
    assert dev["NAS"]["parent"] == box["id"] and dev["NAS"]["port"] == "2" and dev["NAS"]["wired"] is True and dev["NAS"]["alone_on_port"] is True
    assert dev["Kitchen tablet"]["parent"] == box["id"] and dev["Kitchen tablet"]["band"] == "2.4ghz" and dev["Kitchen tablet"]["wired"] is False
    assert dev["Phone"]["parent"] == rep["id"] and dev["Phone"]["band"] == "2.4ghz"
    assert dev["Old laptop"]["online"] is False and dev["Old laptop"]["parent"] is None
    assert box["stats"]["clients"] == 3 and rep["stats"]["clients"] == 1
    assert st["controller"]["class"] == "home_router" and st["controller"]["mesh"] is True and st["controller"]["version"] == "7.57"
    assert "Repeater upstairs" not in dev, "a mesh device is equipment, not an end device"


def test_state_without_a_mesh_puts_everything_on_the_box():
    st = model.to_neutral(raw_of(mesh=None))
    neutral.validate(st)
    box = by_name(st, "equipment")["FRITZ!Box 7590"]
    assert len(st["equipment"]) == 1 and st["controller"]["mesh"] is False
    dev = by_name(st)
    assert dev["NAS"]["parent"] == box["id"] and dev["NAS"]["port"] == "2"
    assert dev["Phone"]["parent"] == box["id"] and dev["Phone"]["wired"] is False and dev["Phone"]["port"] is None
    assert dev["Old laptop"]["parent"] is None
    assert dev["Repeater upstairs"]["parent"] == box["id"]       # without the mesh list a repeater is a host like any


def test_an_offline_repeater_is_offline_equipment_and_a_check():
    st = model.to_neutral(raw_of(mesh=fx.mesh_doc(repeater_up=False), hosts=[h for h in fx.HOSTS if h[0] != fx.REPEATER]))
    rep = by_name(st, "equipment")["Repeater upstairs"]
    assert rep["online"] is False and rep["uplink"] is None
    assert [(f["kind"], f["level"], f["params"]) for f in st["findings"]] == [("mesh_device_offline", "warn", {"device": "Repeater upstairs"})]
    assert by_name(st)["Phone"]["parent"] is None


def test_the_network_and_the_internet_connection():
    st = model.to_neutral(raw_of())
    net = st["networks"][0]
    assert net["cidr"] == "192.168.178.0/24" and net["gateway_ip"] == fx.BOX_IP and net["management"] is True
    assert net["dhcp"] == {"on": True, "range": ["192.168.178.20", "192.168.178.200"]}
    assert all(d["network"] == net["id"] for d in st["devices"])
    assert [(w["type"], w["up"]) for w in st["wans"]] == [("ip", True)]
    down = model.to_neutral(raw_of(wans=[{"kind": "ppp", "status": "Disconnected"}]))
    assert [(w["type"], w["up"]) for w in down["wans"]] == [("ppp", False)]
    assert [(f["kind"], f["level"]) for f in down["findings"]] == [("wan_down", "alert")]
    assert model.to_neutral(raw_of(wans=[{"kind": "ip", "status": "Unconfigured"}]))["wans"] == []


def test_missing_areas_stay_empty_and_nothing_raises():
    for raw in ({}, None, {"hosts": None}, {"info": "x", "lan": 5, "wans": [None, 1], "hosts": [None, {"mac": "bad"}]}):
        st = model.to_neutral(raw)
        neutral.validate(st)
        assert st["devices"] == [] and len(st["equipment"]) == 1 and st["networks"] == [] and st["wans"] == []


def test_a_host_with_a_wrong_address_or_a_repeated_one_is_skipped_or_kept_once():
    hosts = [("zz", "192.168.178.9", "Bad", "Ethernet", 1, True), (fx.NAS, "192.168.178.20", "NAS", "Ethernet", 2, True),
             (fx.NAS.lower(), "192.168.178.21", "NAS again", "Ethernet", 2, True)]
    st = model.to_neutral(raw_of(hosts=hosts, mesh=None))
    assert [d["name"] for d in st["devices"]] == ["NAS"]


def test_two_devices_on_one_port_are_not_alone():
    hosts = [(fx.NAS, "192.168.178.20", "NAS", "Ethernet", 2, True), (fx.PRINTER, "192.168.178.31", "Printer", "Ethernet", 2, True)]
    st = model.to_neutral(raw_of(hosts=hosts, mesh=None))
    assert not any(d["alone_on_port"] for d in st["devices"])


def test_ids_are_opaque_and_no_secret_data_is_kept():
    raw = raw_of()
    st = model.to_neutral(raw)
    text = json.dumps(st).lower()
    for needle in ("02:4c:4c", "02-4c-4c", "serial-do-not-keep", "log line", "203.0.113.9", "0123456789abcdef"):
        assert needle not in text, needle
    ids = [o["id"] for k in ("equipment", "devices", "networks", "wans", "findings") for o in st[k]]
    assert len(ids) == len(set(ids)) and all(i[0] in "ecnwf" and len(i) == 13 for i in ids)


def test_the_state_is_stable_between_two_translations():
    assert json.dumps(model.to_neutral(raw_of()), sort_keys=True) == json.dumps(model.to_neutral(raw_of()), sort_keys=True)


# ---------------------------------------------------------------------------- topology observation
def test_topology_event_follows_the_mesh():
    ev = model.topology_event(raw_of(), 123.0)
    assert ev["type"] == "topology" and ev["source"] == "controller" and ev["t"] == 123.0
    devs = {d["name"]: d for d in ev["devices"]}
    assert devs["FRITZ!Box 7590"]["mac"] == fx.BOX.lower() and devs["FRITZ!Box 7590"]["role"] == "router" and devs["FRITZ!Box 7590"]["uplink"] is None
    assert devs["Repeater upstairs"]["uplink"] == {"mac": fx.BOX.lower(), "port": None, "own_port": None, "medium": "radio"}
    cl = {c["name"]: c for c in ev["clients"]}
    assert set(cl) == {"NAS", "Office printer", "Kitchen tablet", "Phone"}, "only devices that are active now"
    assert cl["NAS"] == {"mac": fx.NAS.lower(), "ip": "192.168.178.20", "name": "NAS", "medium": "wired", "parent_mac": fx.BOX.lower(),
                         "port": "2", "band": None, "ssid": None}
    assert cl["Phone"]["parent_mac"] == fx.REPEATER.lower() and cl["Phone"]["medium"] == "radio" and cl["Phone"]["band"] == "2.4ghz"


def test_topology_event_without_a_mesh_uses_the_box_address_of_its_lan_side():
    ev = model.topology_event(raw_of(mesh=None), 1.0)
    assert [d["name"] for d in ev["devices"]] == ["FRITZ!Box 7590"]
    assert all(c["parent_mac"] == fx.BOX.lower() for c in ev["clients"]) and len(ev["clients"]) == 5


def test_topology_event_without_any_box_address_has_no_clients():
    raw = raw_of(mesh=None)
    raw.pop("lan_mac")
    ev = model.topology_event(raw, 1.0)
    assert ev["devices"] == [] and ev["clients"] == []


def test_firmware_text_of_the_box():
    from lanloom.integrations.fritzbox import read
    assert read.fritzos_version("154.07.57") == "7.57" and read.fritzos_version("113.08.20") == "8.20"
    assert read.fritzos_version("weird") == "weird" and read.fritzos_version(None) is None
