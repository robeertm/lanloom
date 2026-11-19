"""Storage-box card (lanloom/controlroom/nas.py): volumes from agent, SNMP and hypervisor, ring and levels, "no reading" is unknown."""
from types import SimpleNamespace

import pytest

from crhelp import T0, FakeCore
from lanloom.controlroom import nas

GB = 10 ** 9
MIB = 1 << 20
DEV = "d-test-9"
IP = "192.168.50.9"


def add_dev(core, dev_id=DEV, ip=IP, kind="nas", title="Storage box", user_kind=None, hidden=0):
    core.store.upsert_device({"id": dev_id, "ip": ip, "mac": None, "data": {}, "kind": kind, "confidence": 0.9, "title": title}, None)
    if user_kind:
        core.store.execute("UPDATE devices SET user_kind=? WHERE id=?", (user_kind, dev_id))
    if hidden:
        core.store.execute("UPDATE devices SET hidden=1 WHERE id=?", (dev_id,))
    core.ip_to_device[ip] = dev_id


def agent_disks(core, disks, containers=None, dev=DEV):
    items = list(containers or [])
    core.agents = SimpleNamespace(
        device_detail=lambda dev_id, now: {"hosts": [{"system": {"disks": disks}}]} if dev_id == dev else None,
        containers_by_device=lambda now: {dev: [("a1", c) for c in items]} if items else {})


def disk(mount, total, free):
    return {"mount": mount, "total": total, "free": free}


def snmp_vols(core, vols, host=IP, t=T0):
    core.snmp = {1: {"host": host, "t": t, "hr": {"volumes": vols}}}


def vol(name, used, total):
    return {"name": name, "used": used, "total": total, "pct": 0}


def reading(t=T0 - 10, **values):
    return {"t": t, "values": values}


def card_of(core, readings=None, backup_rows=None, ups_row=None, dev_id=DEV):
    dev = core.store.device(dev_id)
    return nas.card(core, dev, T0, readings or {}, backup_rows or [], ups_row)


# ---------------------------------------------------------------------------------------------------- volumes
def test_volume_from_the_agent_with_levels():
    core = FakeCore()
    add_dev(core)
    agent_disks(core, [disk("/volume1", 1000 * GB, 400 * GB), disk("/volume2", 1000 * GB, 100 * GB), disk("/volume3", 1000 * GB, 50 * GB),
                       disk("/volume4", 1000 * GB, 150 * GB)])
    vols = nas.volumes(core, DEV, T0, IP)
    assert [(v["name"], v["pct"], v["level"], v["source"]) for v in vols] == [("/volume3", 95.0, "danger", "agent"), ("/volume2", 90.0, "warn", "agent"),
                                                                           ("/volume4", 85.0, "warn", "agent"), ("/volume1", 60.0, "ok", "agent")]
    assert vols[0]["used"] == 950 * GB and vols[0]["total"] == 1000 * GB


@pytest.mark.parametrize("used,pct,level", [(0, 0.0, "ok"), (849, 84.9, "ok"), (850, 85.0, "warn"), (949, 94.9, "warn"), (950, 95.0, "danger"),
                                            (1000, 100.0, "danger"), (1500, 100.0, "danger")])
def test_ring_levels_at_the_limits(used, pct, level):
    v = nas._vol("v", used * GB, 1000 * GB, "agent")
    assert v["pct"] == pct and v["level"] == level


@pytest.mark.parametrize("used,total", [(None, 1000 * GB), (-1, 1000 * GB), (1, None), (1, 0), (1, 100 * MIB), (1, 256 * MIB - 1), ("x", 1000 * GB), (1, "y")])
def test_a_volume_without_figures_or_too_small_is_not_listed(used, total):
    assert nas._vol("v", used, total, "agent") is None


def test_minimum_size_is_included():
    assert nas._vol("v", 1, nas.MIN_VOLUME, "agent") is not None


def test_system_mounts_and_tiny_disks_are_skipped_but_similar_names_stay():
    core = FakeCore()
    add_dev(core)
    agent_disks(core, [disk("/run", 1000 * GB, 0), disk("/run/user/1000", 1000 * GB, 0), disk("/boot/efi", 1000 * GB, 0), disk("/var/lib/docker/overlay2/x", 1000 * GB, 0),
                       disk("/proc", 1000 * GB, 0), disk("/snap/core", 1000 * GB, 0), disk("/dev/shm", 1000 * GB, 0),
                       disk("/runner-data", 1000 * GB, 500 * GB), disk("/devices", 1000 * GB, 500 * GB), disk("/tiny", 100 * MIB, 0), disk("", 1000 * GB, 0),
                       disk("/nototal", 0, 0), disk("/volume1", 2000 * GB, 1000 * GB)])
    assert sorted(v["name"] for v in nas.volumes(core, DEV, T0, IP)) == ["/devices", "/runner-data", "/volume1"]


def test_snmp_volumes_and_the_same_volume_from_two_sources_is_listed_once():
    core = FakeCore()
    add_dev(core)
    agent_disks(core, [disk("/volume1", 1000 * GB, 500 * GB)])
    snmp_vols(core, [vol("/volume1", 600 * GB, 1000 * GB), vol("/volume2", 100 * GB, 1000 * GB), vol("/run/lock", 5 * GB, 10 * GB), vol("Volume label", 1, 100 * MIB)])
    vols = nas.volumes(core, DEV, T0, IP)
    assert [(v["name"], v["source"]) for v in vols] == [("/volume1", "agent"), ("/volume2", "snmp")]          # the first source wins


def test_snmp_of_another_device_is_not_mine():
    core = FakeCore()
    add_dev(core)
    snmp_vols(core, [vol("/volume1", 1, 1000 * GB)], host="192.168.50.77")
    assert nas.volumes(core, DEV, T0, IP) == []


def test_hypervisor_storage_of_the_hosts_of_the_device():
    core = FakeCore()
    add_dev(core)
    state = {"hosts": [{"id": "h1", "ip": IP}, {"id": "h2", "ip": "192.168.50.99"}],
             "storage": [{"host": "h1", "name": "local-lvm", "used": 300 * GB, "total": 500 * GB}, {"host": "h1", "id": "s2", "used": 10 * GB, "total": 500 * GB},
                         {"host": "h2", "name": "elsewhere", "used": 1, "total": 500 * GB}, {"host": None, "name": "shared", "used": 1, "total": 500 * GB}]}
    core.configs = [{"key": "int:1", "name": "hv", "age_s": 10, "state": state}]
    vols = nas.volumes(core, DEV, T0, IP)
    assert [(v["name"], v["source"]) for v in vols] == [("local-lvm", "hypervisor"), ("s2", "hypervisor")]


def test_volumes_survive_a_broken_agent_and_a_broken_core():
    core = FakeCore()
    add_dev(core)

    def boom(*a, **k):
        raise RuntimeError("x")
    core.agents = SimpleNamespace(device_detail=boom, containers_by_device=boom)
    core.live_configs = boom
    assert nas.volumes(core, DEV, T0, IP) == []
    assert nas.containers(core, DEV, T0) is None and nas.guests(core, DEV) is None


def test_volumes_are_ordered_fullest_first_then_by_name():
    core = FakeCore()
    add_dev(core)
    agent_disks(core, [disk("/b", 1000 * GB, 500 * GB), disk("/a", 1000 * GB, 500 * GB), disk("/c", 1000 * GB, 100 * GB)])
    assert [v["name"] for v in nas.volumes(core, DEV, T0, IP)] == ["/c", "/a", "/b"]


# ---------------------------------------------------------------------------------------------------- containers and guests
def test_container_counts_and_limits():
    core = FakeCore()
    add_dev(core)
    cs = [{"name": "web", "state": "running", "health": "healthy"}, {"name": "db", "state": "running", "health": "unhealthy"},
          {"name": "loop", "state": "restarting"}, {"name": "old", "state": "exited"}]
    agent_disks(core, [], cs)
    ct = nas.containers(core, DEV, T0)
    assert ct["total"] == 4 and ct["running"] == 2 and ct["unhealthy"] == 2 and ct["level"] == "warn" and ct["more"] == 0
    assert [i["name"] for i in ct["items"]] == ["web", "db", "loop", "old"]
    many = [{"name": "c%d" % i, "state": "running", "health": ""} for i in range(20)]
    agent_disks(core, [], many)
    ct = nas.containers(core, DEV, T0)
    assert ct["total"] == 20 and len(ct["items"]) == nas.MAX_ITEMS and ct["more"] == 8 and ct["level"] == "ok"


def test_no_container_card_without_agent_or_containers():
    core = FakeCore()
    add_dev(core)
    assert nas.containers(core, DEV, T0) is None
    agent_disks(core, [], [])
    assert nas.containers(core, DEV, T0) is None


def test_guest_counts_of_the_hypervisor_on_the_box():
    core = FakeCore()
    add_dev(core)
    state = {"hosts": [{"id": "h1", "ip": IP}, {"id": "h2", "ip": "192.168.50.99"}],
             "guests": [{"host": "h1", "name": "vm1", "kind": "vm", "status": "running"}, {"host": "h1", "name": "ct1", "kind": "lxc", "status": "stopped"},
                        {"host": "h2", "name": "foreign", "kind": "vm", "status": "running"}]}
    core.configs = [{"key": "int:1", "name": "hv", "age_s": 10, "state": state}]
    g = nas.guests(core, DEV)
    assert g["total"] == 2 and g["running"] == 1 and [i["name"] for i in g["items"]] == ["vm1", "ct1"]
    core.configs = []
    assert nas.guests(core, DEV) is None


# ---------------------------------------------------------------------------------------------------- vitals
@pytest.mark.parametrize("metric,value,level", [("temp", 69.9, "ok"), ("temp", 70, "warn"), ("temp", 84.9, "warn"), ("temp", 85, "danger"),
                                                ("mem", 84.9, "ok"), ("mem", 85, "warn"), ("mem", 94.9, "warn"), ("mem", 95, "danger"),
                                                ("cpu", 20, "ok"), ("cpu", 90, "warn"), ("cpu", 99, "danger")])
def test_vital_levels(metric, value, level):
    assert nas.vitals({"agent": reading(**{metric: value})})[metric]["level"] == level


def test_vitals_take_the_newest_source_per_metric_and_skip_junk():
    out = nas.vitals({"snmp": reading(T0 - 100, cpu=10.0, temp=40.0), "agent": reading(T0 - 10, cpu=20.0, mem="x", temp=None)})
    assert out["cpu"] == {"value": 20.0, "source": "agent", "level": "ok"} and out["temp"]["source"] == "snmp" and "mem" not in out
    assert nas.vitals({}) == {}


# ---------------------------------------------------------------------------------------------------- the card
def test_card_without_any_reading_is_unknown_never_ok():
    core = FakeCore()
    add_dev(core)
    c = card_of(core)
    assert c["level"] == "unknown" and c["reading"] is False and c["volumes"] == [] and c["ring"] == {"pct": None, "level": "unknown", "volume": ""}
    assert c["sources"] == [] and c["title"] == "Storage box" and c["ip"] == IP


def test_card_with_fresh_readings_and_a_healthy_volume_is_ok():
    core = FakeCore()
    add_dev(core)
    agent_disks(core, [disk("/volume1", 1000 * GB, 600 * GB)])
    c = card_of(core, {"agent": reading(T0 - 30, cpu=10.0, mem=40.0, temp=45.0)})
    assert c["level"] == "ok" and c["reading"] is True and c["ring"] == {"pct": 40.0, "level": "ok", "volume": "/volume1"}
    assert c["sources"] == [{"source": "agent", "age_s": 30.0, "fresh": "fresh"}] and c["vitals"]["temp"]["value"] == 45.0


def test_card_with_only_stale_readings_is_unknown_even_when_every_value_looks_fine():
    core = FakeCore()
    add_dev(core)
    agent_disks(core, [disk("/volume1", 1000 * GB, 600 * GB)])
    c = card_of(core, {"agent": reading(T0 - 2000, cpu=10.0, temp=45.0)})
    assert c["level"] == "unknown" and c["vitals"] == {}                      # stale vitals do not count as ok figures
    assert c["sources"][0]["fresh"] == "stale"


def test_card_level_is_the_worst_of_volumes_vitals_containers_backups_and_ups():
    core = FakeCore()
    add_dev(core)
    agent_disks(core, [disk("/volume1", 1000 * GB, 600 * GB)], [{"name": "x", "state": "running", "health": "unhealthy"}])
    fresh = {"agent": reading(T0 - 10, temp=45.0)}
    assert card_of(core, fresh)["level"] == "warn"                             # the unhealthy container
    agent_disks(core, [disk("/volume1", 1000 * GB, 600 * GB)])
    assert card_of(core, fresh)["level"] == "ok"
    assert card_of(core, {"agent": reading(T0 - 10, temp=90.0)})["level"] == "danger"
    assert card_of(core, fresh, backup_rows=[{"device": DEV, "level": "danger"}])["level"] == "danger"
    assert card_of(core, fresh, backup_rows=[{"device": DEV, "level": "unknown"}, {"device": "other", "level": "danger"}])["level"] == "ok"
    assert card_of(core, fresh, ups_row={"level": "warn"})["level"] == "warn"
    assert card_of(core, fresh, ups_row={"level": "unknown"})["level"] == "ok"
    assert card_of(core, fresh, backup_rows=[{"device": DEV, "level": "warn"}])["backups"] == [{"device": DEV, "level": "warn"}]


def test_a_full_volume_makes_the_ring_and_the_card_red():
    core = FakeCore()
    add_dev(core)
    agent_disks(core, [disk("/volume1", 1000 * GB, 30 * GB), disk("/volume2", 1000 * GB, 800 * GB)])
    c = card_of(core, {"agent": reading(T0 - 10, cpu=1.0)})
    assert c["ring"] == {"pct": 97.0, "level": "danger", "volume": "/volume1"} and c["level"] == "danger"


def test_title_falls_back_to_the_user_title_ip_and_id():
    core = FakeCore()
    add_dev(core, title="Auto title")
    core.store.execute("UPDATE devices SET user_title='My box' WHERE id=?", (DEV,))
    assert card_of(core)["title"] == "My box"


# ---------------------------------------------------------------------------------------------------- the list of cards
class Health:
    def __init__(self, data):
        self.data = data

    def readings(self, now):
        return self.data


def test_only_storage_boxes_and_not_hidden_or_merged_devices_get_a_card():
    core = FakeCore()
    add_dev(core, "d-nas1", "192.168.50.11", title="Box one")
    add_dev(core, "d-nas2", "192.168.50.12", title="Box two", kind="computer", user_kind="nas")
    add_dev(core, "d-pc", "192.168.50.13", title="A PC", kind="computer")
    add_dev(core, "d-hid", "192.168.50.14", title="Hidden box", hidden=1)
    add_dev(core, "d-port2", "192.168.50.15", title="Second port of box one")
    add_dev(core, "d-user", "192.168.50.16", title="Not a box any more", kind="nas", user_kind="computer")
    core.merged = {"d-port2"}
    core.hosthealth = Health({"d-nas1": {"agent": reading(T0 - 10, cpu=5.0)}, "d-nas2": {}})
    out = nas.cards(core, T0, [], None)
    assert [(c["title"], c["level"]) for c in out] == [("Box two", "unknown"), ("Box one", "ok")]


def test_cards_are_sorted_worst_first_and_a_silent_box_before_a_calm_one():
    core = FakeCore()
    add_dev(core, "d-a", "192.168.50.21", title="Alpha")
    add_dev(core, "d-b", "192.168.50.22", title="Bravo")
    add_dev(core, "d-c", "192.168.50.23", title="Charlie")
    core.hosthealth = Health({"d-a": {"agent": reading(T0 - 10, cpu=5.0)}, "d-b": {"agent": reading(T0 - 10, temp=95.0)}})
    out = nas.cards(core, T0, [], None)
    assert [(c["title"], c["level"]) for c in out] == [("Bravo", "danger"), ("Charlie", "unknown"), ("Alpha", "ok")]


def test_cards_use_the_ups_of_the_box_and_survive_a_failing_health_source():
    core = FakeCore()
    add_dev(core)
    seen = []

    class Book:
        def runtime_for(self, dev_id, now):
            seen.append(dev_id)
            return {"level": "warn", "state": "battery"}

    def boom(now):
        raise RuntimeError("x")
    core.hosthealth = SimpleNamespace(readings=boom)
    out = nas.cards(core, T0, [], Book())
    assert seen == [DEV] and out[0]["ups"]["state"] == "battery" and out[0]["level"] == "warn"


def test_is_nas_prefers_the_choice_of_a_person():
    assert nas.is_nas({"kind": "nas"}) and not nas.is_nas({"kind": "nas", "user_kind": "computer"}) and nas.is_nas({"kind": "computer", "user_kind": "nas"})
    assert not nas.is_nas({})
