"""The hypervisor family inside the app: core bookkeeping, topology (guests below their node), the component
card, the VLAN chain across sources, the night report contribution and the API (fixed clock, no network)."""
import copy
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import core, integrations, mapmodel, topology
from lanloom.firewall.editor import Editor
from lanloom.integrations.proxmox import model, read, report
from lanloom.main import create_app
from lanloom.store import Store

from . import fixtures as fx

HDR = {"X-Lanloom": "1"}
USER = {"id": 1, "username": "admin"}


def make_event(raw=None, t=None):
    r = read.ProxmoxReader(url="https://192.0.2.1:8006", token_id="lanloom@pve!read", token_secret="0b1c2d3e-4f50")
    return r.config_event(fx.raw() if raw is None else raw, fx.NOW if t is None else t)   # fixed clock for ages


def add_device(c, did, ip, mac):
    """An inventory row as a scan would leave it (all required columns), indexed by address like the core does."""
    import json
    now = time.time()
    cols = [r["name"] for r in c.store.query("PRAGMA table_info(devices)")]
    row = {"id": did, "ip": ip, "mac": mac, "data": json.dumps({"ip": ip, "mac": mac}), "kind": "unknown", "confidence": 0.5,
           "title": did, "first_seen": now, "last_seen": now}
    for r in c.store.query("PRAGMA table_info(devices)"):
        if r["notnull"] and r["dflt_value"] is None and r["name"] not in row:
            row[r["name"]] = 0 if "INT" in (r["type"] or "").upper() or "REAL" in (r["type"] or "").upper() else ""
    keys = [k for k in row if k in cols]
    c.store.execute("INSERT INTO devices(%s) VALUES(%s)" % (",".join(keys), ",".join("?" * len(keys))), tuple(row[k] for k in keys))
    c.ip_to_device[ip] = did
    c.structure_version += 1


def add_integration(store, integ_id=1, name="Rack hosts", kind="proxmox"):
    store.execute("INSERT INTO integrations(id, kind, name, config, enabled, created) VALUES(?,?,?,?,1,?)",
                  (integ_id, kind, name, "{}", time.time()))


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    return tmp_path


@pytest.fixture()
def booked(env):
    store = Store(":memory:")
    c = core.Core(config_mod.load(), store, None)
    add_integration(store)
    return c


# ---------------------------------------------------------------------------- registry and manifest
def test_family_is_built_in_with_a_read_half_only():
    m = integrations.get("proxmox")
    assert m.read == "lanloom.integrations.proxmox.read:ProxmoxReader" and m.write == "" and m.docs == "docs/integrations/proxmox.md"
    assert m.capabilities["write"] == {} and m.secret_fields == ("token_secret",)
    assert (Path(__file__).resolve().parents[3] / m.docs).is_file()
    assert core.integration_fields("proxmox") == (("url", "token_id", "allow_self_signed"), ("token_secret",))
    with pytest.raises(PermissionError):
        integrations.load_write("proxmox")


def test_night_report_contribution_is_registered():
    fams = dict(integrations.night_sections())
    assert fams["proxmox"] is report.night_section
    assert integrations.load_model("proxmox") is model
    with pytest.raises(KeyError):
        integrations.load_model("nothing")


# ---------------------------------------------------------------------------- core
def test_config_event_is_booked_and_listed(booked):
    booked.on_event("int:1", 1, make_event())
    live = booked.live_configs()
    assert [e["key"] for e in live] == ["int:1"] and live[0]["family"] == "proxmox" and live[0]["caps"]["write"] == {}
    assert len(live[0]["state"]["guests"]) == 5


def test_guests_hang_below_their_node(booked):
    for n, (ip, m) in enumerate((("192.168.50.10", "02:4c:4c:00:00:0a"), ("192.168.50.80", "02:4c:4c:00:01:00"),
                                ("192.168.50.53", "02:4c:4c:00:02:00"), ("192.168.50.11", "02:4c:4c:00:00:0b"))):
        add_device(booked, "d%d" % n, ip, m)
    booked.on_event("int:1", 1, model.topology_event(fx.raw(), time.time()))
    links = booked.topology()["links"]
    assert links["d1"]["parent"] == "d0" and links["d1"]["medium"] == "virtual" and links["d1"]["source"] == "hypervisor"
    assert links["d1"]["vars"]["port"] == "vmbr0, VLAN 20" and links["d1"]["reason"] == "topo.reason.hypervisor"
    assert links["d2"]["parent"] == "d3"                         # the container on the second node, found by its address


def test_resolver_hypervisor_beats_controller_and_is_never_refined():
    devs = [{"id": "gw", "ip": "192.168.50.1", "mac": "02:00:00:00:00:01", "kind": "router", "title": "gw", "hostnames": [], "is_gateway": True},
            {"id": "sw", "ip": "192.168.50.2", "mac": "02:00:00:00:00:02", "kind": "switch", "title": "sw", "hostnames": [], "is_gateway": False},
            {"id": "host", "ip": "192.168.50.10", "mac": "02:00:00:00:00:0a", "kind": "server", "title": "host", "hostnames": [], "is_gateway": False},
            {"id": "g1", "ip": "192.168.50.81", "mac": "02:00:00:00:00:51", "kind": "unknown", "title": "g1", "hostnames": [], "is_gateway": False},
            {"id": "g2", "ip": "192.168.50.82", "mac": "52:54:00:00:00:52", "kind": "unknown", "title": "g2", "hostnames": [], "is_gateway": False}]
    t = 1_000_000.0
    ctl = {"type": "topology", "source": "controller", "t": t, "devices": [],
           "clients": [{"mac": m, "parent_mac": "02:00:00:00:00:02", "port": "5", "medium": "wired"}
                       for m in ("02:00:00:00:00:0a", "02:00:00:00:00:51", "52:54:00:00:00:52")]}
    hv = {"type": "topology", "source": "hypervisor", "t": t, "hosts": [{"name": "node-a", "ip": "192.168.50.10"}],
          "guests": [{"mac": "02:00:00:00:00:51", "host": "node-a", "bridge": "vmbr0"}, {"mac": "52:54:00:00:00:52", "host": "node-a", "bridge": "vmbr0", "vlan": 30}]}
    topo = topology.resolve(devs, {}, {"a": {"integ": 1, "ev": ctl}, "b": {"integ": 2, "ev": hv}}, now=t)
    assert topo["links"]["host"]["parent"] == "sw" and topo["links"]["host"]["port"] == "5"
    for g in ("g1", "g2"):
        assert topo["links"][g]["parent"] == "host" and topo["links"][g]["source"] == "hypervisor" and topo["links"][g]["medium"] == "virtual"
        assert [c["source"] for c in topo["links"][g]["conflicts"]] == ["controller"]
    assert topo["links"]["g2"]["vars"]["vlan"] == 30


def test_unknown_node_becomes_a_server_node():
    devs = [{"id": "g1", "ip": "192.168.50.81", "mac": "02:00:00:00:00:51", "kind": "unknown", "title": "g1", "hostnames": [], "is_gateway": False}]
    hv = {"type": "topology", "source": "hypervisor", "t": 1.0, "hosts": [{"name": "far-node", "ip": "10.9.9.9"}],
          "guests": [{"ip": "192.168.50.81", "host": "far-node"}, {"ip": "192.168.50.99", "host": "nowhere"}]}
    topo = topology.resolve(devs, {}, {"b": {"integ": 2, "ev": hv}}, now=1.0)
    assert topo["links"]["g1"]["parent"] == "hv:far-node" and topo["nodes"]["hv:far-node"]["kind"] == "server"


def test_vlan_chain_finding_joins_the_controller_ports(booked):
    add_integration(booked.store, 2, "Hall console", "controller")          # any source whose state has switch ports
    ip_of = {"d0": "192.168.50.10", "dsw": "192.168.50.2"}
    for did, ip in ip_of.items():
        add_device(booked, did, ip, "02:4c:4c:00:00:%s" % ("0a" if did == "d0" else "02"))
    booked.on_event("int:3", 3, {"type": "topology", "source": "controller", "t": time.time(), "devices": [],
                                 "clients": [{"mac": "02:4c:4c:00:00:0a", "parent_mac": "02:4c:4c:00:00:02", "port": "7", "medium": "wired"}]})
    sw_state = {"v": 2, "zones": [], "networks": [{"id": "n30", "vlan": 30}], "devices": [], "allow": [],
                "equipment": [{"id": "e1", "name": "Switch hall", "mgmt_ip": "192.168.50.2"}],
                "ports": [{"id": "p7", "device": "e1", "idx": 7, "name": "Port 7", "native_vlan": 1, "tagged": ["n30"]}]}
    booked.live_cfg[2] = {"state": sw_state, "caps": {}, "family": "controller", "t": time.time()}
    booked.on_event("int:1", 1, make_event())
    chain = [f for f in booked.live_cfg[1]["state"]["findings"] if f["kind"] == "vlan_chain"]
    assert [(f["params"]["guest"], f["params"]["vlan"], f["params"]["port"]) for f in chain] == [("web", 20, "7"), ("camera-recorder", 150, "7")]


def test_component_card_of_a_node_and_of_a_guest(booked, monkeypatch):
    booked.on_event("int:1", 1, make_event())
    host_dev = {"id": "d0", "ip": "192.168.50.10", "mac": "02:4c:4c:00:00:0a"}
    out = mapmodel.hypervisor_detail(booked, "d0", host_dev)
    hv = out["hypervisor"]
    assert hv["name"] == "Rack hosts" and hv["host"]["name"] == "node-a" and hv["cluster"] == "lab"
    assert [g["vmid"] for g in hv["guests"]] == [100, 101, 9000] and "rrd" not in hv["guests"][0] and hv["guests"][0]["nics"]
    assert {s["name"] for s in hv["storage"]} == {"local", "local-lvm", "backup-share"}
    assert {f["kind"] for f in hv["findings"]} == {"storage_full", "vlan_not_allowed", "backup_failed", "backup_old"}
    assert "guest" not in out
    by_mac = mapmodel.hypervisor_detail(booked, "d1", {"id": "d1", "ip": "192.168.50.199", "mac": "02:4C:4C:00:01:00"})["guest"]
    assert by_mac["guest"]["vmid"] == 100 and by_mac["host"]["name"] == "node-a" and by_mac["guest"]["rrd"]
    assert by_mac["firewall"]["enabled"] and len(by_mac["rules"]) == 3 and [n["name"] for n in by_mac["nics"]] == ["net0", "net1"]
    by_ip = mapmodel.hypervisor_detail(booked, "d2", {"id": "d2", "ip": "192.168.50.53", "mac": None})["guest"]
    assert by_ip["guest"]["name"] == "dns"
    assert mapmodel.hypervisor_detail(booked, "d9", {"id": "d9", "ip": "192.168.50.250", "mac": "02:4c:4c:00:09:99"}) == {}
    assert "02:4c:4c" not in repr(out).lower() and "02:4c:4c" not in repr(by_mac).lower()


def test_editor_live_view_of_the_hypervisor(env, booked):
    ed = Editor(config_mod.load(), booked.store)
    ed.live = booked.live_configs
    booked.on_event("int:1", 1, make_event())
    v = ed.view(USER, "192.168.50.20", source="int:1")
    assert v["mode"] == "live" and v["capabilities"]["write"] == {} and v["live"]["family"] == "proxmox"
    assert v["state"]["hosts"] and v["state"]["zones"] == [] and v["requester"] is None


# ---------------------------------------------------------------------------- night report contribution
def test_night_section_first_night_lists_findings_only():
    st = model.to_neutral(fx.raw(), fx.NOW)
    sec = report.night_section(None, st, fx.NOW, "Rack hosts")
    assert sec["family"] == "proxmox" and sec["title"] == "night.hv.title" and sec["name"] == "Rack hosts"
    keys = [i["key"] for i in sec["items"]]
    assert keys[:3] == ["hv.finding.bridge_missing", "hv.finding.storage_full", "hv.finding.vlan_not_allowed"]
    assert "night.hv.backup_failed" in keys and "hv.finding.backup_failed" not in keys
    assert sec["look"] == 5 and sec["summary"] == "night.hv.summary_look" and sec["params"]["n"] == 5
    assert "02:4c:4c" not in repr(sec).lower() and "gddd" not in repr(sec)                   # no hardware addresses, no internal ids


def test_night_section_compares_with_the_last_report():
    prev = model.to_neutral(fx.raw(), fx.NOW - fx.DAY)
    prev["t"] = fx.NOW - fx.DAY
    raw = fx.raw()
    for r in raw["resources"]:
        if r.get("id") == "lxc/200":
            r["status"] = "stopped"
    raw["resources"] = [r for r in raw["resources"] if r.get("id") != "lxc/201"]
    raw["resources"].append({"id": "qemu/102", "type": "qemu", "vmid": 102, "node": "node-b", "name": "new-box", "status": "running"})
    raw["cluster"][2]["online"] = 0
    sec = report.night_section(prev, model.to_neutral(raw, fx.NOW), fx.NOW)
    items = {(i["key"], i["params"].get("guest") or i["params"].get("host")) for i in sec["items"]}
    assert ("night.hv.guest_stopped", "dns") in items and ("night.hv.guest_gone", "old-test") in items
    assert ("night.hv.guest_new", "new-box") in items and ("night.hv.node_offline", "node-b") in items
    assert sec["items"][0]["level"] == "alert"


def test_night_section_calm_night_and_no_source():
    raw = fx.raw()
    raw["nodes"]["node-a"]["tasks"] = []
    raw["nodes"]["node-a"]["storage"][1]["used"] = 10 * 2**30
    raw["guests"]["qemu/101"]["config"]["net0"] = "e1000=02:4C:4C:00:01:10,bridge=vmbr0,tag=50"
    raw["guests"]["lxc/201"]["config"]["net0"] = "name=eth0,bridge=vmbr0,hwaddr=02:4C:4C:00:02:01,ip=dhcp"
    raw["backups"].append({"vmid": 101, "ctime": fx.NOW - 3600, "size": 1, "storage": "backup-share"})
    st = model.to_neutral(raw, fx.NOW)
    sec = report.night_section(None, st, fx.NOW)
    assert sec["look"] == 0 and sec["summary"] == "night.hv.summary_calm" and sec["params"] == {"n": 0, "running": 3, "total": 4, "nodes": 2}
    assert [i["key"] for i in sec["items"]] == ["night.hv.backup_ok"] and sec["items"][0]["params"] == {"n": 3}
    assert report.night_section(None, None, fx.NOW) is None and report.night_section(None, {"zones": []}, fx.NOW) is None


def test_night_texts_exist_in_every_language():
    import json
    root = Path(__file__).resolve().parents[3] / "lanloom" / "i18n"
    langs = sorted(p for p in root.glob("*.json") if not p.name.startswith("board."))
    keys = {"night.hv.title", "night.hv.summary_look", "night.hv.summary_calm", "night.hv.backup_ok", "night.hv.backup_failed",
            "night.hv.guest_new", "night.hv.guest_gone", "night.hv.guest_stopped", "night.hv.node_offline"}
    keys |= {"hv.finding.%s" % k for k in ("node_offline", "storage_full", "backup_none", "backup_old", "backup_failed", "bridge_missing",
                                            "vlan_not_allowed", "fw_inactive", "vlan_chain")}
    for p in langs:
        d = json.loads(p.read_text(encoding="utf-8"))
        assert keys <= set(d), (p.name, sorted(keys - set(d)))


# ---------------------------------------------------------------------------- API
def sign_in(c, data_dir):
    code = (Path(data_dir) / "setup-code.txt").read_text().strip()
    r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"}, headers=HDR)
    assert r.status_code == 200


def test_api_lists_the_family_and_serves_the_live_view(env):
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, env)
        fams = {f["family"]: f for f in c.get("/api/integrations").json()["families"]}
        assert fams["proxmox"]["sample"] == "hypervisor" and fams["proxmox"]["label"] == "Sample hypervisor (local API)"
        assert all(f["sample"] is True for k, f in fams.items() if k != "proxmox") and fams["proxmox"]["capabilities"]["write"] == {}
        assert [f["k"] for f in fams["proxmox"]["form"]] == ["url", "token_id", "token_secret", "allow_self_signed"]
        ll = c.app.state.ll
        add_integration(ll.store, 41)
        ll.core.on_event("int:41", 41, make_event())
        body = c.get("/api/firewall", params={"source": "int:41"}).json()
        assert body["mode"] == "live" and body["state"]["hosts"] and body["capabilities"]["write"] == {}
        assert "02:4c:4c" not in repr(body).lower()
