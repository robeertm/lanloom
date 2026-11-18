"""Gateway changes in the firewall editor (stage B): networks and VLANs, fixed addresses and port forwards - drafts,
preview with lockout rules and confirmations, plan, gate, simulation, undo, roles and the audit log - plus the
segmentation plan (target network per device, progress, nightly section) and what uses a device by its address. The
rehearsal target is the sample gateway of the sample network."""
import time

import pytest
from fastapi.testclient import TestClient

from lanloom import addrusers, config as config_mod
from lanloom.firewall import adapter, netconf, report as fw_report, segplan
from lanloom.main import create_app
from lanloom.nightly.contrib import Ctx
from lanloom.sources import hub_deps

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
ORIGIN = "http://lanloom.test"
NOW = 1_800_000_000


def gw():
    s = adapter.with_sample_gateway(adapter.with_sample_hypervisor(adapter.sample_state(), now=NOW))
    s["dhcp_reservations"] = adapter.sample_reservations(s)
    return s


def pv_of(s, ops, **kw):
    return netconf.preview(s, netconf.clean_ops(s, ops), **kw)


def dev(s, name):
    return next(d for d in s["devices"] if d["name"] == name)


# ---------------------------------------------------------------------------- pure rules
def test_suggestion_follows_the_scheme_and_never_overlaps():
    s = gw()
    sug = netconf.suggest(s, "iot")
    assert sug["cidr"] == "192.168.31.0/24" and sug["vlan"] == 31          # 30 is taken by Media
    assert sug["dhcp"] == {"on": True, "range": ["192.168.31.100", "192.168.31.199"]}
    assert sug["zone"] == "z_internal"
    assert netconf.suggest(s, "cameras")["vlan"] == 71


@pytest.mark.parametrize("after, key", [
    ({"name": "x", "cidr": "8.8.8.0/24", "vlan": 90}, "fw.block.net_cidr"),               # not private
    ({"name": "x", "cidr": "192.168.90.5/24", "vlan": 90}, "fw.block.net_cidr"),          # not the network address
    ({"name": "x", "cidr": "10.0.0.0/8", "vlan": 90}, "fw.block.net_cidr"),               # too large
    ({"name": "x", "cidr": "192.168.50.0/25", "vlan": 90}, "fw.block.net_overlap"),
    ({"name": "x", "cidr": "192.168.90.0/24", "vlan": 30}, "fw.block.net_vlan_taken"),
    ({"name": "x", "cidr": "192.168.90.0/24", "vlan": 90, "dhcp": {"on": True, "range": ["192.168.90.1", "192.168.90.50"]}},
     "fw.block.dhcp_range"),                                                               # holds the gateway address
])
def test_new_network_rules(after, key):
    s = gw()
    pv = pv_of(s, [{"kind": "network.create", "after": after}])
    assert [b[0] for b in pv["blocks"]] == [key]


def test_management_and_foreign_networks_are_never_removed():
    s = gw()
    pv = pv_of(s, [{"kind": "network.update", "object": "n_mgmt", "after": {"name": "Admin"}},
                   {"kind": "network.delete", "object": "n_media"}])
    assert sorted(b[0] for b in pv["blocks"]) == ["fw.block.management", "fw.block.not_own"]


def test_address_range_and_address_service_of_a_network_with_devices():
    s = gw()
    pv = pv_of(s, [{"kind": "network.update", "object": "n_guest", "after": {"dhcp": {"on": False, "range": None}}}])
    assert [b[0] for b in pv["blocks"]] == ["fw.block.dhcp_in_use"]      # the visitor phone takes a dynamic address
    pv = pv_of(s, [{"kind": "network.update", "object": "n_media", "after": {"dhcp": {"on": False, "range": None}}}])
    assert not pv["blocks"] and pv["warnings"] == [["fw.warn.dhcp_off", {"name": "Media"}]]   # every device fixed
    pv = pv_of(s, [{"kind": "network.update", "object": "n_media", "after": {"cidr": "192.168.33.0/24"}}])
    assert [b[0] for b in pv["blocks"]] == ["fw.block.network_in_use"]
    pv = pv_of(s, [{"kind": "network.update", "object": "n_guest",
                    "after": {"dhcp": {"on": True, "range": ["192.168.80.150", "192.168.80.199"]}}}])
    assert not pv["blocks"] and pv["warnings"] == [["fw.warn.dhcp_range", {"name": "Guests", "n": 1}]]


def test_fixed_address_rules():
    s = gw()
    tv = dev(s, "Living room TV")
    res = next(r for r in s["dhcp_reservations"] if r["client"] == tv["id"])
    pv = pv_of(s, [{"kind": "dhcp.reserve", "after": {"client": tv["id"], "ip": "192.168.30.90"}}])
    assert [b[0] for b in pv["blocks"]] == ["fw.block.reservation_exists"]
    pv = pv_of(s, [{"kind": "dhcp.release", "object": res["id"]}])
    assert not pv["blocks"] and pv["warnings"][0][0] == "fw.warn.fixed_ip_removed"
    visitor = dev(s, "Visitor phone")
    for ip, key in (("192.168.50.99", "fw.block.ip_outside"), ("192.168.80.1", "fw.block.ip_reserved"),
                    ("192.168.80.255", "fw.block.ip_reserved")):
        pv = pv_of(s, [{"kind": "dhcp.reserve", "after": {"client": visitor["id"], "ip": ip}}])
        assert [b[0] for b in pv["blocks"]] in ([key], ["fw.block.network_kind"]), (ip, pv["blocks"])
    gateway = dev(s, "Gateway")
    pv = pv_of(s, [{"kind": "dhcp.release", "object": "r_" + gateway["id"]}])
    assert [b[0] for b in pv["blocks"]] == ["fw.block.protected"]


@pytest.mark.parametrize("after, key", [
    ({"dst_ip": "192.168.50.1", "wan_port": "8080"}, "fw.block.forward_gateway"),
    ({"dst_ip": "192.168.10.2", "wan_port": "8080"}, "fw.block.management"),       # the core switch
    ({"dst_ip": "203.0.113.20", "wan_port": "8080"}, "fw.block.forward_target"),
    ({"dst_ip": "192.168.90.20", "wan_port": "8080"}, "fw.block.forward_target"),  # no local network
    ({"dst_ip": "192.168.50.20", "wan_port": "8443"}, "fw.block.forward_clash"),   # the photo sharing forward
    ({"dst_ip": "192.168.50.20", "wan_port": "8000-8010", "dst_port": "80"}, "fw.block.forward_ports"),
])
def test_port_forward_rules(after, key):
    s = gw()
    pv = pv_of(s, [{"kind": "forward.create", "after": dict({"name": "x", "proto": "tcp"}, **after)}])
    assert [b[0] for b in pv["blocks"]] == [key]


def test_a_new_forward_needs_its_own_confirmation_but_switching_off_does_not():
    s = gw()
    pv = pv_of(s, [{"kind": "forward.create", "after": {"name": "Web", "proto": "tcp", "wan_port": "8080", "dst_ip": "192.168.50.20",
                                                      "dst_port": "80"}}])
    assert not pv["blocks"] and len(pv["confirm"]) == 1
    assert pv["warnings"][0][0] == "fw.warn.forward_open" and pv["warnings"][0][1]["confirm"] == pv["confirm"][0]
    pv = pv_of(s, [{"kind": "forward.update", "object": "f_photos", "after": {"enabled": False}}])
    assert not pv["blocks"] and pv["confirm"] == []          # closing never needs a confirmation of its own
    pv = pv_of(s, [{"kind": "forward.update", "object": "f_game", "after": {"enabled": True}}])
    assert not pv["blocks"] and len(pv["confirm"]) == 1


def test_two_new_networks_in_one_plan_must_not_collide():
    s = gw()
    pv = pv_of(s, [{"kind": "network.create", "object": "new:aaaaaaaaaaaaaaaa", "after": {"name": "a", "cidr": "192.168.91.0/24", "vlan": 91}},
                   {"kind": "network.create", "object": "new:bbbbbbbbbbbbbbbb", "after": {"name": "b", "cidr": "192.168.92.0/24", "vlan": 91}}])
    assert [b[0] for b in pv["blocks"]] == ["fw.block.net_vlan_taken"]


def test_what_uses_a_device_by_address_turns_into_its_own_warning():
    s = gw()
    plug = dev(s, "Washer plug")
    printer = dev(s, "Office printer")
    deps = {plug["id"]: [{"via": "integration", "by": "Washer plug (plug)"}], printer["id"]: [{"by": "Office laptop"}]}
    pv = pv_of(s, [{"kind": "dhcp.release", "object": "r_" + plug["id"]}, {"kind": "dhcp.release", "object": "r_" + printer["id"]}],
               deps=deps)
    keys = [w[0] for w in pv["warnings"]]
    assert "fw.warn.addr_integration" in keys and "fw.warn.dependency" in keys


# ---------------------------------------------------------------------------- segmentation plan (pure)
class _Store:
    def __init__(self):
        self.s = {}

    def get_setting(self, k, d=None):
        return self.s.get(k, d)

    def set_setting(self, k, v):
        self.s[k] = v


def test_segmentation_plan_progress_labels_and_moves():
    s = gw()
    store = _Store()
    segplan.set_targets(store, adapter.SAMPLE_SEGPLAN, now=NOW)
    st = segplan.status(s, segplan.load(store))
    assert (st["total"], st["moved"], st["pending"]) == (6, 1, 5)
    iot = next(g for g in st["targets"] if g["label"] == "IoT")
    assert iot == {"key": "l:iot", "label": "IoT", "net": None, "total": 4, "moved": 0}
    assert st["devices"]["d19"]["planned"] is True and st["devices"]["d14"]["moved"] is True
    assert st["moves"] == {"d11": "n_media"}                 # the IoT network does not exist yet
    # once a network named like the label exists, its devices can be drafted
    s["networks"].append({"id": "n_iot", "name": "IoT", "zone": "z_internal", "cidr": "192.168.31.0/24", "vlan": 31,
                          "management": False, "purpose": "iot"})
    st = segplan.status(s, segplan.load(store))
    assert set(st["moves"]) == {"d11", "d19", "d20", "d22", "d23"}
    segplan.set_targets(store, {"d19": None, "d99": {"label": "Cameras"}}, now=NOW)
    st = segplan.status(s, segplan.load(store))
    assert st["total"] == 5 and st["elsewhere"] == 1


@pytest.mark.parametrize("bad", [{"net": ""}, {"label": "  "}, {"label": "a\x01"}, {"zone": "x"}, "IoT", {"net": "a", "label": "b"}])
def test_segmentation_plan_refuses_odd_targets(bad):
    with pytest.raises(ValueError):
        segplan.set_targets(_Store(), {"d1": bad})


def test_nightly_section_of_the_plan_with_a_fixed_clock():
    store = _Store()

    class Core:
        pass
    core = Core()
    core.store = store
    ctx = Ctx(core=core, journal=None, since=NOW - 86400, until=NOW, prev=None)
    assert not fw_report._plan_available(ctx)
    s = gw()
    segplan.set_targets(store, adapter.SAMPLE_SEGPLAN, now=NOW)
    segplan.remember(store, segplan.status(s, segplan.load(store)), now=NOW)
    assert fw_report._plan_available(ctx)
    sec = fw_report._plan_build(ctx)
    assert sec["id"] == "netplan" and sec["summary"]["key"] == "report.netplan.summary"
    assert sec["summary"]["vars"] == {"moved": 1, "total": 6}
    assert [i["key"] for i in sec["items"]] == ["report.netplan.group", "report.netplan.group"]
    ctx.prev = {"sections": [{"id": "netplan", "summary": {"key": "x", "vars": {"moved": 0, "total": 6}}}]}
    sec = fw_report._plan_build(ctx)
    prog = [i for i in sec["items"] if i["key"] == "report.netplan.progress"]
    assert len(prog) == 1 and prog[0]["vars"] == {"n": 1} and prog[0]["level"] == "ok"


# ---------------------------------------------------------------------------- what uses an address
def test_hub_configuration_keeps_only_private_addresses():
    cfg = {"triggers": [{"trigger": "time", "at": "00:00"}],
           "actions": [{"action": "shell_command.ping", "data": {"host": "192.168.60.62"}},
                       {"action": "rest_command.x", "data": {"url": "http://10.1.2.3:8080/api?v=1.2.3.4"}},
                       {"action": "notify.x", "data": {"message": "public 8.8.8.8, version 2.10.0.1"}}]}
    refs = hub_deps.refs_from_config(cfg, set())
    assert refs["ips"] == ["10.1.2.3", "192.168.60.62"]
    assert "ips" not in hub_deps.refs_from_config({"actions": []}, set())


def test_address_users_from_hub_snapshot_dependencies_and_own_sources():
    class Core:
        def integrations(self):
            return [{"id": 1, "kind": "hub", "name": "House hub", "config": {"url": "https://192.168.60.14:8123"}},
                    {"id": 2, "kind": "snmp", "name": "Switch", "config": {"host": "192.168.10.2"}}]
    core = Core()
    core.hub = {1: {"snapshot": {"devices": [{"name": "Washer plug", "domains": ["plug"], "ip_hint": "192.168.60.60"},
                                             {"name": "Weather", "service": True, "ip_hint": "192.168.60.99"}],
                                 "trackers": [{"mac": None, "ip": "192.168.50.22"}],
                                 "entities": [{"entity_id": "automation.energy", "name": "Energy report"}]},
                    "deps": {"automations": {"automation.energy": {"ips": ["192.168.60.62"]}}}}}
    out = addrusers.address_users(core)
    assert out["192.168.60.60"] == [{"via": "integration", "by": "Washer plug (plug)"}]
    assert "192.168.60.99" not in out                       # a service, not a device on the network
    assert out["192.168.50.22"] == [{"via": "tracker", "by": "House hub"}]
    assert out["192.168.60.62"] == [{"via": "automation", "by": "Energy report"}]
    assert out["192.168.60.14"] == [{"via": "lanloom", "by": "House hub"}]
    assert out["192.168.10.2"] == [{"via": "lanloom", "by": "Switch"}]

    class Broken:
        def integrations(self):
            raise RuntimeError("x")
    assert addrusers.address_users(Broken()) == {}


# ---------------------------------------------------------------------------- through the API
@pytest.fixture()
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.setenv("LANLOOM_FW_SIM_WATCH_S", "0")
    monkeypatch.setenv("LANLOOM_FW_SIM_SETTLE_S", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url=ORIGIN) as client:
        code = (tmp_path / "setup-code.txt").read_text().strip()
        assert client.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200
        client.ll = app.state.ll
        segplan.set_targets(app.state.ll.firewall.store,
                            adapter.SAMPLE_SEGPLAN)           # what the demo seeds on its first minute
        yield client


def wait_done(c):
    for _ in range(600):
        v = c.get("/api/firewall?source=editor").json()
        if not v["running"]:
            return v
        time.sleep(0.05)
    raise AssertionError("simulation did not finish")


def test_demo_seeds_the_plan_once():
    class Core:
        pass
    core = Core()
    core.store = _Store()
    from lanloom import demo
    demo.seed_segplan(core)
    assert set(core.store.s["fw_segplan"]["targets"]) == set(adapter.SAMPLE_SEGPLAN)
    segplan.set_targets(core.store, {k: None for k in adapter.SAMPLE_SEGPLAN})
    demo.seed_segplan(core)                                 # an emptied plan stays empty
    assert core.store.s["fw_segplan"]["targets"] == {}


def test_editor_offers_gateway_changes_templates_and_the_plan(c):
    v = c.get("/api/firewall?source=editor").json()
    assert v["mode"] == "simulation"
    assert {"network.create", "dhcp.release", "forward.create"} <= set(v["capabilities"]["write"])
    assert [t["template"] for t in v["netconf"]["templates"]] == ["iot", "cameras", "work", "lan"]
    assert v["netconf"]["refs"]["n_media"]["devices"] == 3
    assert {f["id"] for f in v["state"]["port_forwards"]} == {"f_photos", "f_game"}
    assert v["state"]["dhcp_reservations"]
    seg = v["segplan"]
    assert (seg["total"], seg["moved"]) == (6, 1) and seg["devices"]["d19"]["label"] == "IoT"
    assert v["uses"]["d19"] == [{"by": "Washer plug (plug)", "via": "integration"}]


def test_create_a_network_for_the_plan_then_undo(c):
    v = c.get("/api/firewall?source=editor").json()
    tpl = next(t for t in v["netconf"]["templates"] if t["template"] == "iot")
    op = {"kind": "network.create", "after": {"name": "IoT", "cidr": tpl["cidr"], "vlan": tpl["vlan"], "purpose": "iot"}}
    d = c.put("/api/firewall/draft", json={"ops": [op]}, headers=HDR).json()
    assert len(d["ops"]) == 1
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert not pv["blocks"] and pv["changes"][0]["what"] == "net.change.new"
    assert [w[0] for w in pv["warnings"]][:2] == ["fw.warn.net_new", "fw.warn.net_zone"]
    p = c.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR).json()
    assert c.post("/api/firewall/approve", json={"plan": p["plan"]["id"]}, headers=HDR).json()["ok"]
    v = wait_done(c)
    job = v["history"][0]
    assert job["status"] == "done", job
    new = next(n for n in v["state"]["networks"] if n["name"] == "IoT")
    assert new["own"] is True and new["vlan"] == tpl["vlan"] and new["zone"] == "z_internal"
    assert v["segplan"]["devices"]["d19"]["possible"] is True and "d19" in v["segplan"]["moves"]
    d = c.post("/api/firewall/undo", json={"plan": job["plan"]}, headers=HDR).json()
    assert [o["kind"] for o in d["ops"].values()] == ["network.delete"]


def test_port_forward_needs_its_confirmation_through_the_api(c):
    op = {"kind": "forward.create", "after": {"name": "Web", "proto": "tcp", "wan_port": "8080", "dst_ip": "192.168.50.20", "dst_port": "80"}}
    c.put("/api/firewall/draft", json={"ops": [op]}, headers=HDR)
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert not pv["blocks"] and len(pv["confirm"]) == 1
    r = c.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR)
    assert r.status_code == 409 and r.json()["error"] == "fw.block.confirm"
    p = c.post("/api/firewall/plan", json={"confirmed": pv["confirm"]}, headers=HDR).json()
    assert c.post("/api/firewall/approve", json={"plan": p["plan"]["id"]}, headers=HDR).json()["ok"]
    v = wait_done(c)
    assert v["history"][0]["status"] == "done"
    assert any(f["name"] == "Web" and f["own"] for f in v["state"]["port_forwards"])


def test_release_of_a_fixed_address_warns_about_what_uses_it(c):
    c.put("/api/firewall/draft", json={"ops": [{"kind": "dhcp.release", "object": "r_d19"}]}, headers=HDR)
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert not pv["blocks"]
    assert ["fw.warn.addr_integration", {"name": "Washer plug", "by": "Washer plug (plug)"}] in pv["warnings"]


def test_segplan_route_roles_and_audit(c):
    r = c.put("/api/firewall/segplan", json={"targets": {"d07": {"label": "Servers"}, "d19": None}}, headers=HDR)
    assert r.status_code == 200 and "d07" in r.json()["targets"] and "d19" not in r.json()["targets"]
    assert c.put("/api/firewall/segplan", json={"targets": {"d07": {"zone": "x"}}}, headers=HDR).json()["error"] == "segplan.err.target"
    v = c.get("/api/firewall?source=editor").json()
    assert v["segplan"]["devices"]["d07"] == {"label": "Servers", "net": None, "moved": False, "possible": False, "planned": True}
    entries = c.get("/api/audit?limit=30").json()["entries"]
    assert any(e["action"] == "firewall.segplan" and e["detail"] == {"devices": 2, "removed": 1} for e in entries)
    c.ll.accounts.create("vic", PW, role="viewer")
    c.cookies.clear()
    assert c.post("/api/login", json={"username": "vic", "password": PW}, headers=HDR).status_code == 200
    assert c.put("/api/firewall/segplan", json={"targets": {}}, headers=HDR).status_code == 403
    assert c.put("/api/firewall/draft", json={"ops": [{"kind": "dhcp.release", "object": "r_d19"}]}, headers=HDR).status_code == 403
