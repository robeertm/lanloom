"""Translation of host/cluster replies into the neutral model (pure functions, fixed clock)."""
import copy

from lanloom.firewall import neutral
from lanloom.integrations.proxmox import model

from . import fixtures as fx


def state(raw=None):
    return model.to_neutral(fx.raw() if raw is None else raw, fx.NOW)


def by(items, **kw):
    return next(i for i in items if all(i.get(k) == v for k, v in kw.items()))


def test_full_state_is_valid_and_complete():
    st = state()
    neutral.validate(st)
    assert st["zones"] == [] and st["devices"] == [] and st["networks"] == []          # nothing for the zone board
    assert [h["name"] for h in st["hosts"]] == ["node-a", "node-b"]
    assert [g["vmid"] for g in st["guests"]] == [100, 101, 200, 201, 9000]
    assert st["controller"] == {"version": "8.2.4", "apis": ["token"], "kind": "hypervisor", "cluster": "lab", "quorate": True, "nodes": 2}


def test_hosts():
    a = by(state()["hosts"], name="node-a")
    assert a["ip"] == "192.168.50.10" and a["online"] and a["local"] and a["version"] == "8.2.4"
    assert a["cpu"] == 0.12 and a["cores"] == 8 and a["mem_total"] == 32 * 2**30 and a["load"] == [0.42, 0.38, 0.3]
    assert a["guests_running"] == 2 and a["guests_total"] == 2                         # the template does not count


def test_guests_hang_below_their_host_and_carry_facts():
    st = state()
    a = by(st["hosts"], name="node-a")
    web = by(st["guests"], vmid=100)
    assert web["host"] == a["id"] and web["kind"] == "vm" and web["status"] == "running" and web["tags"] == ["web", "prod"]
    assert web["ips"] == ["192.168.50.80"] and web["agent"] and web["firewall"] is True and web["onboot"]
    assert web["traffic"] == {"in_bps": 1200, "out_bps": 4800}
    assert len(web["rrd"]["t"]) == model.RRD_POINTS and web["rrd"]["t"] == sorted(web["rrd"]["t"])
    rec = by(st["guests"], vmid=101)
    assert rec["protected"] == "guest.protection" and rec["agent"] is False and rec["firewall"] is None
    dns = by(st["guests"], vmid=200)
    assert dns["kind"] == "lxc" and dns["ips"] == ["192.168.50.53"] and dns["firewall"] is False and dns["pool"] == "infra"
    assert by(st["guests"], vmid=9000)["template"] is True


def test_guest_ids_survive_a_migration():
    raw = fx.raw()
    before = by(state(raw)["guests"], vmid=100)["id"]
    for r in raw["resources"]:
        if r.get("id") == "qemu/100":
            r["node"] = "node-b"
    after = by(state(raw)["guests"], vmid=100)
    assert after["id"] == before and after["host"] == by(state(raw)["hosts"], name="node-b")["id"]


def test_network_cards():
    st = state()
    web = by(st["guests"], vmid=100)
    nics = [n for n in st["guest_nics"] if n["guest"] == web["id"]]
    assert [(n["name"], n["model"], n["bridge"], n["vlan"], n["firewall"]) for n in nics] == \
        [("net0", "virtio", "vmbr0", 20, True), ("net1", "virtio", "vmbr1", None, False)]
    rec = by(st["guest_nics"], guest=by(st["guests"], vmid=101)["id"])
    assert rec["model"] == "e1000" and rec["vlan"] == 150 and rec["rate_mbs"] == 12.5
    dns = by(st["guest_nics"], guest=by(st["guests"], vmid=200)["id"])
    assert dns["ifname"] == "eth0" and dns["ip"] == "192.168.50.53" and dns["model"] == "veth"


def test_hardware_addresses_never_leave_the_model_unhashed():
    st = state()
    text = repr(st).lower()
    assert "02:4c:4c" not in text and "024c4c" not in text
    hashes = {n["mac_hash"] for n in st["guest_nics"]}
    assert model.mac_hash("02:4C:4C:00:01:00") in hashes and None not in hashes
    assert "cipassword" not in text and "ssh-ed25519" not in text and "private note" not in text


def test_bridges_bonds_and_management():
    st = state()
    a = by(st["hosts"], name="node-a")["id"]
    vmbr0 = by(st["bridges"], host=a, name="vmbr0")
    assert vmbr0["kind"] == "bridge" and vmbr0["vlan_aware"] and vmbr0["vids"] == "2-100" and vmbr0["ports"] == ["bond0"]
    assert vmbr0["cidr"] == "192.168.50.10/24" and vmbr0["mgmt"] and vmbr0["vnets"] == ["cams", "iot"]
    bond = by(st["bridges"], host=a, name="bond0")
    assert bond["kind"] == "bond" and bond["ports"] == ["eno1", "eno2"] and bond["bond_mode"] == "802.3ad"
    assert by(st["bridges"], host=a, name="vmbr1")["mgmt"] is False


def test_sdn():
    st = state()
    assert [(z["name"], z["type"], z["bridge"]) for z in st["sdn_zones"]] == [("lan", "vlan", "vmbr0")]
    cams = by(st["sdn_vnets"], name="cams")
    assert cams["tag"] == 150 and cams["alias"] == "Cameras" and cams["subnets"] == [
        {"cidr": "192.168.150.0/24", "gateway": "192.168.150.1", "snat": False, "dhcp": False}]


def test_firewall_at_every_level():
    st = state()
    scopes = sorted({(r["scope"], r["owner"]) for r in st["fw_rules"]})
    a = by(st["hosts"], name="node-a")["id"]
    assert scopes == sorted([("dc", "datacenter"), ("group", "webservers"), ("node", a)])
    ssh = by(st["fw_rules"], scope="dc", pos=0)
    assert ssh["macro"] == "SSH" and ssh["action"] == "allow" and ssh["source"] == "+management" and ssh["iface"] == "vmbr0"
    web = by(st["guests"], vmid=100)["id"]
    g_rules = [r for r in st["guest_rules"] if r["owner"] == web]
    assert [(r["direction"], r["action"], r["enabled"]) for r in g_rules] == [("in", "allow", True), ("group", "group", True), ("in", "block", False)]
    assert by(g_rules, pos=1)["group"] == "webservers"
    dc = by(st["fw_options"], scope="dc")
    assert dc["enabled"] and dc["policy_in"] == "DROP"
    assert by(st["fw_options"], scope="guest", owner=web)["macfilter"] is True
    areas = sorted((x["area"], x["title"]) for x in st["raw"])
    assert ("fw_ipset", "management") in areas and ("fw_alias", "office") in areas and ("fw_group", "webservers") in areas
    blk = by(st["raw"], area="fw_ipset", title="blocklist")
    assert blk["count"] == 2 and blk["detail"] == "203.0.113.0/24, !203.0.113.7" and blk["editable"] is False


def test_storage_shared_once():
    st = state()
    names = sorted((s["name"], s["host"] is None) for s in st["storage"])
    assert names == [("backup-share", True), ("local", False), ("local", False), ("local-lvm", False)]
    lvm = by(st["storage"], name="local-lvm")
    assert lvm["fraction"] == 0.95 and lvm["content"] == ["images", "rootdir"]


def test_backups_and_jobs():
    st = state()
    web = by(st["guests"], vmid=100)
    assert web["backup"]["last_t"] == fx.NOW - fx.DAY and web["backup"]["age_s"] == fx.DAY and web["backup"]["verified"]
    assert web["backup"]["in_job"] and web["backup"]["storage"] == "backup-share"
    old = by(st["guests"], vmid=201)
    assert old["backup"]["in_job"] is False and old["backup"]["last_t"] is None
    job = st["backup_jobs"][0]
    assert job["selection"] == "all" and job["enabled"] and old["id"] in job["excluded"] and old["id"] not in job["guests"]
    assert st["backup_jobs"][1]["selection"] == "list" and st["backup_jobs"][1]["enabled"] is False


def test_findings_with_a_fixed_clock():
    kinds = [(f["kind"], f["level"]) for f in state()["findings"]]
    assert kinds == [("bridge_missing", "alert"), ("storage_full", "alert"), ("vlan_not_allowed", "alert"),
                     ("backup_failed", "warn"), ("backup_old", "warn")]
    st = state()
    f = by(st["findings"], kind="vlan_not_allowed")
    assert f["params"] == {"guest": "camera-recorder", "vmid": 101, "nic": "net0", "bridge": "vmbr0", "vlan": 150, "host": "node-a"}


def test_findings_move_with_the_clock_not_the_wall():
    raw = fx.raw()
    later = model.to_neutral(raw, fx.NOW + 3 * fx.DAY)            # the failed backup leaves the 48 h window
    assert "backup_failed" not in [f["kind"] for f in later["findings"]]
    assert by(later["guests"], vmid=100)["backup"]["age_s"] == 4 * fx.DAY


def test_more_findings():
    raw = fx.raw()
    raw["cluster"][2]["online"] = 0
    raw["dc_fw"]["options"]["enable"] = 0
    raw["backups"] = [b for b in raw["backups"] if b["vmid"] != 200]
    raw["backup_jobs"] = []
    kinds = {f["kind"] for f in model.to_neutral(raw, fx.NOW)["findings"]}
    assert {"node_offline", "fw_inactive", "backup_none"} <= kinds


def test_empty_and_hostile_input_never_raises():
    assert model.to_neutral({}, fx.NOW)["hosts"] == []
    assert model.to_neutral(None, fx.NOW)["guests"] == []
    junk = {"resources": [1, "x", {"type": "qemu", "vmid": "abc"}, {"type": "lxc", "vmid": 5, "node": None}],
            "guests": {"qemu/1": "x", "../etc": {}}, "nodes": {"n": {"network": [{"iface": None}, "x"]}},
            "dc_fw": {"rules": [{"pos": "x"}], "groups": "y"}, "sdn": {"vnets": [{"vnet": "v", "tag": "q"}]},
            "backups": [{"vmid": 5, "ctime": "never"}], "cluster": [{"type": "node"}]}
    st = model.to_neutral(junk, fx.NOW)
    neutral.validate(st)
    assert [g["vmid"] for g in st["guests"]] == [5]          # the cluster list is the truth, not left-over configs


def test_parsers():
    assert model.parse_kv("virtio=AA:BB,bridge=vmbr0,tag=5") == {"virtio": "AA:BB", "bridge": "vmbr0", "tag": "5"}
    assert model.parse_kv("1,fstrim_cloned_disks=1") == {"_": "1", "fstrim_cloned_disks": "1"}
    assert model.vlan_list("10;20;30-32") == [10, 20, 30, 31, 32]
    assert model.vlan_list("0;5000;x") == []
    assert model.tags_of("a;b c,d") == ["a", "b", "c", "d"]


def test_topology_event_carries_nodes_and_cards():
    ev = model.topology_event(fx.raw(), fx.NOW)
    assert ev["type"] == "topology" and ev["source"] == "hypervisor"
    assert ev["hosts"] == [{"name": "node-a", "ip": "192.168.50.10", "online": True}, {"name": "node-b", "ip": "192.168.50.11", "online": True}]
    web = [g for g in ev["guests"] if g["name"] == "web"]
    assert web[0] == {"mac": "02:4c:4c:00:01:00", "ip": "192.168.50.80", "name": "web", "host": "node-a", "bridge": "vmbr0", "vlan": 20,
                      "running": True, "kind": "vm"}
    assert not [g for g in ev["guests"] if g["name"] == "tpl-base" and g["running"]]


def test_single_node_without_cluster_takes_the_api_address():
    raw = fx.raw()
    raw["cluster"] = [{"type": "node", "name": "node-a", "online": 1, "local": 1}]
    raw["resources"] = [r for r in raw["resources"] if r.get("node") != "node-b"]
    raw["nodes"].pop("node-b")
    st = model.to_neutral(raw, fx.NOW)
    assert [(h["name"], h["ip"]) for h in st["hosts"]] == [("node-a", "192.168.50.10")] and st["controller"]["cluster"] is None


def test_vlan_chain_across_sources():
    st = state()
    a = by(st["hosts"], name="node-a")["id"]
    sw = {"equipment": [{"id": "e1", "name": "Switch hall", "mgmt_ip": "192.168.50.2"}],
          "networks": [{"id": "n20", "vlan": 20}, {"id": "n30", "vlan": 30}],
          "ports": [{"id": "p7", "device": "e1", "idx": 7, "name": "Port 7", "native_vlan": 1, "tagged": ["n30"]}]}
    out = model.vlan_chain(st, {a: {"ip": "192.168.50.2", "port": "7"}}, [sw])
    assert [(f["kind"], f["params"]["vlan"], f["params"]["guest"]) for f in out] == [("vlan_chain", 20, "web"), ("vlan_chain", 150, "camera-recorder")]
    assert out[0]["params"]["switch"] == "Switch hall" and out[0]["params"]["port"] == "7"
    sw2 = copy.deepcopy(sw)
    sw2["ports"][0]["tagged"] = ["n20", "n30"]
    assert [f["params"]["vlan"] for f in model.vlan_chain(st, {a: {"ip": "192.168.50.2", "port": "Port 7"}}, [sw2])] == [150]
    sw2["ports"][0]["tagged"] = ["*"]
    assert model.vlan_chain(st, {a: {"ip": "192.168.50.2", "port": "7"}}, [sw2]) == []
    sw2["ports"][0]["tagged"] = None                              # unknown settings: nothing is guessed
    assert model.vlan_chain(st, {a: {"ip": "192.168.50.2", "port": "7"}}, [sw2]) == []
    assert model.vlan_chain(st, {}, [sw]) == []
