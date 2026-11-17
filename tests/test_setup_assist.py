"""Setup assistant 2.0: network proposal, adapter suggestions, first picture, sample network, error categories."""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lanloom import classify, config as config_mod, demo, integrations as registry, setup_assist
from lanloom.core import Core
from lanloom.main import create_app
from lanloom.scan import netinfo, ports, safety
from lanloom.store import Store

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"


# ---------------------------------------------------------------------- network proposal
def test_small_network_is_proposed_as_is():
    assert setup_assist.propose_range("192.168.1.0/24", "192.168.1.5", []) == "192.168.1.0/24"
    assert setup_assist.propose_range("10.1.0.0/22", "10.1.1.5", []) == "10.1.0.0/22"


def test_large_network_is_narrowed_around_host_and_neighbours():
    assert setup_assist.propose_range("10.0.0.0/16", "10.0.5.9", ["10.0.5.1", "10.0.5.200"]) == "10.0.5.0/24"
    assert setup_assist.propose_range("10.0.0.0/16", "10.0.5.9", ["10.0.4.1"]) == "10.0.4.0/23"
    assert setup_assist.propose_range("10.0.0.0/16", "10.0.5.9", ["10.0.6.1"]) == "10.0.4.0/22"
    # a neighbour far away never widens beyond /22
    assert setup_assist.propose_range("10.0.0.0/16", "10.0.5.9", ["10.0.200.1"]) == "10.0.4.0/22"
    assert setup_assist.propose_range("10.0.0.0/16", None, []) is None


def test_every_proposal_passes_the_scan_safety_rules():
    nets = [{"cidr": "10.0.0.0/8", "iface": "eth0", "self_ip": "10.20.30.40", "gateway": "10.20.30.1", "host_mode": True},
            {"cidr": "192.168.7.0/24", "iface": "eth1", "self_ip": "192.168.7.2", "gateway": None, "host_mode": True},
            {"cidr": "100.64.0.0/24", "iface": "tun0", "self_ip": None, "gateway": None, "host_mode": True}]
    routes = [{"iface": "eth0", "dest": "172.20.0.0", "gateway": "10.20.30.1", "mask": "255.255.255.0",
               "flags": netinfo.RTF_UP | netinfo.RTF_GATEWAY, "metric": 0},
              {"iface": "eth0", "dest": "172.16.0.0", "gateway": "10.20.30.1", "mask": "255.240.0.0",
               "flags": netinfo.RTF_UP | netinfo.RTF_GATEWAY, "metric": 0},
              {"iface": "eth0", "dest": "0.0.0.0", "gateway": "10.20.30.1", "mask": "0.0.0.0",
               "flags": netinfo.RTF_UP | netinfo.RTF_GATEWAY, "metric": 0}]
    out = setup_assist.build_proposal(nets, routes, ["10.20.30.1"])
    by = {e["cidr"]: e for e in out}
    assert by["10.20.30.0/24"]["suggested"] and by["10.20.30.0/24"]["narrowed"]
    assert not by["192.168.7.0/24"]["suggested"]                     # no gateway: offered, not ticked
    assert by["172.20.0.0/24"]["routed"] and not by["172.20.0.0/24"]["suggested"]
    assert "172.16.0.0/12" not in by                                  # routed range larger than /22: not offered
    for e in out:
        if e["suggested"]:
            assert safety.check_cidr(e["cidr"]) == (True, safety.OK)
            assert e["size"] <= 1024
    assert out[0]["cidr"] == "10.20.30.0/24"                          # proposed first
    cgnat = [e for e in out if e["cidr"].startswith("100.")]
    assert cgnat and not cgnat[0]["allowed"] and not cgnat[0]["suggested"]


def test_single_network_without_gateway_is_proposed_when_neighbours_exist():
    nets = [{"cidr": "172.31.77.0/24", "iface": "eth0", "self_ip": "172.31.77.60", "gateway": None, "host_mode": True}]
    assert setup_assist.build_proposal(nets, [], ["172.31.77.2"])[0]["suggested"]
    assert setup_assist.build_proposal(nets, [], [])[0]["suggested"]       # the only network


def test_resolver_and_lease_parsing_keeps_private_servers_only():
    text = "# comment\nnameserver 127.0.0.53\nnameserver 192.168.1.1\nnameserver 8.8.8.8\nnameserver 10.0.0.2\nsearch lan\n"
    assert setup_assist.parse_resolvers(text) == ["192.168.1.1", "10.0.0.2"]
    leases = ['lease {\n  option dhcp-server-identifier 192.168.1.1;\n}', "ADDRESS=192.168.1.5\nSERVER_ADDRESS=192.168.1.1\n",
              "option dhcp-server-identifier 1.2.3.4;"]
    assert setup_assist.parse_leases(leases) == ["192.168.1.1"]


# ---------------------------------------------------------------------- suggestions
def _core():
    cfg = SimpleNamespace(data_dir=None, demo=False, probe=False)
    c = Core(cfg, Store(":memory:"), None)
    return c


def _host(ip, mac, ports_=(), mdns=(), gateway=False, banners=None, sources=("arp",)):
    return {"ip": ip, "mac": mac, "hostnames": [], "sources": list(sources), "mdns": list(mdns), "ssdp": [], "is_gateway": gateway,
            "ports": [{"port": p, "proto": "tcp", "service": ports.service_name(p), "banner": (banners or {}).get(p)} for p in ports_]}


def _family(caps_guests: bool, port_alone: bool = None):
    """A built-in family by its kind of API (controller or hypervisor) and by its signs (``port_alone``: one sign is
    an open port alone), never by name."""
    return next(f for f in registry.families(selectable_only=True) if ("guests" in f.capabilities["read"]) == caps_guests and f.detect
                and (port_alone is None or any(set(d) == {"port"} for d in f.detect) == port_alone))


def _sign(fam, *keys):
    return next(d for d in fam.detect if set(d) == set(keys))


HUB_TYPE = next(t for t, (k, w) in classify.MDNS_RULES.items() if k == "hub" and w >= 0.8)


def test_suggestions_from_ports_titles_and_announcements():
    ctl, hv, fw = _family(False, True), _family(True), _family(False, False)
    ctl_port, ctl_title = _sign(ctl, "port")["port"], _sign(ctl, "title")["title"]
    hv_sign = _sign(hv, "port", "title")
    fw_sign = _sign(fw, "port", "title")
    c = _core()
    c.ingest({"hosts": [
        _host("192.168.9.1", "02:00:00:00:00:01", (53, 80), gateway=True),
        _host("192.168.9.20", "02:00:00:00:00:20", (8123,), mdns=[{"type": HUB_TYPE, "name": "hub", "port": 8123}]),
        _host("192.168.9.21", "02:00:00:00:00:21", (1883,)),
        _host("192.168.9.22", "02:00:00:00:00:22", (ctl_port,), banners={ctl_port: {"title": "Sample " + ctl_title.title(), "server": None}}),
        _host("192.168.9.23", "02:00:00:00:00:23", (22, hv_sign["port"]),
              banners={hv_sign["port"]: {"title": "node1 - " + hv_sign["title"].title(), "server": None}}),
        _host("192.168.9.24", "02:00:00:00:00:24", (443, fw_sign["port"]),
              banners={fw_sign["port"]: {"title": fw_sign["title"].title() + " - admin console", "server": None}}),
        _host("192.168.9.30", "02:00:00:00:00:30", (80, 443)),
        _host("192.168.9.31", "02:00:00:00:00:31", (fw_sign["port"],)),   # the port alone is no sign of the firewall family
    ]}, None)
    sg = setup_assist.suggestions(c)
    got = {(s["kind"], s["ip"]): s for s in sg}
    assert ("hub", "192.168.9.20") in got and got[("hub", "192.168.9.20")]["certainty"] == "sure"
    assert got[("hub", "192.168.9.20")]["prefill"] == {"url": "http://192.168.9.20:8123"}
    assert got[("mqtt", "192.168.9.21")]["prefill"] == {"host": "192.168.9.21", "port": 1883, "tls": False}
    assert got[(ctl.family, "192.168.9.22")]["certainty"] == "sure" and got[(ctl.family, "192.168.9.22")]["guide"] == "family"
    assert got[(ctl.family, "192.168.9.22")]["prefill"]["url"] == "https://192.168.9.22:%d" % ctl_port
    assert got[(hv.family, "192.168.9.23")]["guide"] == "hv" and got[(hv.family, "192.168.9.23")]["certainty"] == "sure"
    assert got[(fw.family, "192.168.9.24")]["guide"] == "family"
    assert got[(fw.family, "192.168.9.24")]["prefill"]["url"] == "https://192.168.9.24:%d" % fw_sign["port"]
    assert (fw.family, "192.168.9.31") not in got
    assert got[("snmp", "192.168.9.1")]["reasons"][0]["key"] == "setup.reason.gateway"
    assert not [s for s in sg if s["ip"] == "192.168.9.30"]           # a plain web server is no suggestion
    for s in sg:                                                        # never a credential in a prefill
        assert not set(s["prefill"]) & {"password", "token", "api_key", "token_secret", "community"}


def test_configured_source_is_marked_and_sorted_last():
    c = _core()
    c.ingest({"hosts": [_host("192.168.9.21", "02:00:00:00:00:21", (1883,)),
                        _host("192.168.9.20", "02:00:00:00:00:20", (8123,))]}, None)
    c.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                    ("mqtt", "Broker", '{"host": "192.168.9.21", "port": 1883}', time.time()))
    sg = setup_assist.suggestions(c)
    assert sg[-1]["kind"] == "mqtt" and sg[-1]["configured"] is True
    assert sg[0]["kind"] == "hub" and sg[0]["configured"] is False


def test_family_detect_signs_are_validated():
    caps = dict(_family(False).capabilities, family="x_test")
    with pytest.raises(registry.ManifestError):
        registry.IntegrationManifest(family="x_test", label="X", capabilities=caps, detect=({"port": 70000},)).validate()
    with pytest.raises(registry.ManifestError):
        registry.IntegrationManifest(family="x_test", label="X", capabilities=caps, detect=({"title": "Upper"},)).validate()
    with pytest.raises(registry.ManifestError):
        registry.IntegrationManifest(family="x_test", label="X", capabilities=caps, detect=({"colour": "x"},)).validate()
    assert all(m.detect for m in registry.families(selectable_only=True))
    assert _family(True).public()["detect"]


def test_hypervisor_port_is_part_of_the_gentle_scan():
    assert 8006 in ports.GENTLE_PORTS and ports.WEB_PORTS[8006] is True


@pytest.mark.parametrize("key,cat", [("source.hub.auth_failed", "rejected"), ("source.family.token_refused", "rejected"),
                                     ("source.mqtt.tls", "certificate"), ("source.snmp.timeout", "unreachable"),
                                     ("source.controller.refused", "unreachable"), ("source.hub.protocol", "other"),
                                     ("integration.err.can_write", "other"), ("source.hub.ok", "ok"), (None, "other")])
def test_error_categories(key, cat):
    assert setup_assist.error_category(key) == cat


# ---------------------------------------------------------------------- first picture
def test_preview_adds_new_devices_only_and_records_the_first_picture_time():
    c = _core()
    c.store.set_setting("setup_started", time.time() - 42)
    c.ingest({"hosts": [_host("192.168.9.5", "02:00:00:00:00:05", (22,))]}, None)
    c.scan_state = {"state": "running", "id": 7}
    before = c.store.device("d-020000000005")["data"]["ports"]
    n = c._preview({"hosts": [_host("192.168.9.5", "02:00:00:00:00:05"), _host("192.168.9.6", "02:00:00:00:00:06")]}, 7)
    assert n == 1                                                      # the known device keeps its richer data
    assert c.store.device("d-020000000005")["data"]["ports"] == before
    assert c.scan_state["preview"] == 2
    assert 41 <= c.store.get_setting("setup_first_picture_s") <= 60


def test_demo_scan_delivers_a_preview_before_the_ports():
    c = _core()
    c.demo = demo.Demo()
    seen = []

    async def go():
        return await c.demo.scan({"cidrs": ["192.168.50.0/24"]}, lambda p: seen.append(p["phase"]),
                                 preview=lambda r: seen.append(("preview", len(r["hosts"]))))
    asyncio.run(go())
    i = next(i for i, x in enumerate(seen) if isinstance(x, tuple))
    assert "ports" not in seen[:i] and seen[i][1] == len(demo.HOSTS)


# ---------------------------------------------------------------------- API: discover, suggestions, sample network
@pytest.fixture()
def real_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "0")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        c.data_dir = tmp_path
        c.ll = app.state.ll
        yield c


def _account(c):
    code = (Path(c.data_dir) / "setup-code.txt").read_text().strip()
    r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR)
    assert r.status_code == 200, r.text


def test_discover_starts_the_clock_and_proposes_safe_ranges(real_client):
    c = real_client
    assert c.get("/api/setup/discover").status_code == 401
    _account(c)
    r = c.get("/api/setup/discover")
    assert r.status_code == 200
    d = r.json()
    assert set(d) >= {"networks", "gateway", "dns", "dhcp", "host_mode"}
    for n in d["networks"]:
        if n["suggested"]:
            assert safety.check_cidr(n["cidr"])[0] and n["size"] <= 1024
    t0 = c.ll.store.get_setting("setup_started")
    assert isinstance(t0, float)
    c.get("/api/setup/discover")
    assert c.ll.store.get_setting("setup_started") == t0               # the clock starts once
    s = c.get("/api/setup/suggestions").json()
    assert s["suggestions"] == [] and s["guides"]["hub"] == 4 and s["timing"]["started"] == t0


def test_sample_network_is_read_only_and_leaves_the_inventory_alone(real_client):
    c = real_client
    _account(c)
    assert c.get("/api/devices").json()["devices"] == []
    st = c.get("/api/state").json()
    assert st["demo"] is False and st["setup_done"] is False
    r = c.post("/api/setup/demo", json={"on": True}, headers=HDR)
    assert r.status_code == 200 and r.json()["demo_view"] is True
    st = c.get("/api/state").json()
    assert st["demo"] is True and st["demo_view"] is True and st["setup_done"] is True
    devs = c.get("/api/devices").json()["devices"]
    assert len(devs) == len(demo.HOSTS) + len(demo.ADAPTER_CLIENTS) + len(demo.ADAPTER_FDB)   # the sample adapters list a few more
    assert all(d["ip"].startswith("192.168.") or not d["ip"] for d in devs)
    assert sum(1 for d in devs if d["ip"].startswith("192.168.50.")) == len(demo.HOSTS)
    assert c.get("/api/map").json()["components"]
    # nothing can be changed from the sample
    r = c.patch("/api/devices/" + devs[0]["id"], json={"title": "x"}, headers=HDR)
    assert r.status_code == 409 and r.json()["error"] == "demo.err.read_only"
    assert c.post("/api/scan", json={"cidrs": ["192.168.50.0/24"]}, headers=HDR).status_code == 409
    assert c.put("/api/settings", json={"setup_done": True}, headers=HDR).status_code == 409
    # leave: the real (empty) inventory is back, the sample never reached the real store
    r = c.post("/api/setup/demo", json={"on": False}, headers=HDR)
    assert r.status_code == 200
    st = c.get("/api/state").json()
    assert st["demo"] is False and st["demo_view"] is False and st["setup_done"] is False
    assert c.get("/api/devices").json()["devices"] == []
    assert c.ll.store.one("SELECT COUNT(*) AS n FROM devices")["n"] == 0
    assert c.ll.store.one("SELECT COUNT(*) AS n FROM integrations")["n"] == 0


def test_sample_cookie_without_session_gives_nothing(real_client):
    c = real_client
    _account(c)
    c.post("/api/setup/demo", json={"on": True}, headers=HDR)
    c.post("/api/logout", json={}, headers=HDR)
    assert c.get("/api/devices").status_code == 401
    assert c.get("/api/state").json()["demo_view"] is False


def test_integration_test_names_what_to_look_at(real_client):
    c = real_client
    _account(c)
    r = c.post("/api/integrations/test", json={"kind": "mqtt", "config": {"host": "127.0.0.1", "port": 9}, "secrets": {}}, headers=HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["category"] in ("unreachable", "other")
