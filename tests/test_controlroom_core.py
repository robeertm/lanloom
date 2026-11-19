"""How the core feeds the control room: NUT connections (settings, events, removal), SNMP frames with a UPS-MIB, gateway events,
and the sample integrations (the new source kind does not disturb the counts of the demo)."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lanloom import core as core_mod
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.store import Store

T = 1_800_000_000.0


def make(tmp_path, demo=False):
    store = Store(":memory:")
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=demo, probe=True, release_feed="", image_repo="",
                          cookie_secure="never", trusted_origins=(), trusted_proxies=())
    return core_mod.Core(cfg, store, Vault(store, b"k" * KEY_BYTES))


# ---------------------------------------------------------------------------------------------------- settings of a NUT connection
def test_nut_settings_are_cleaned():
    cfg, sec = core_mod.Core.clean_integration("nut", {"host": " 192.0.2.10 ", "port": "3494", "ups": "office", "junk": 1}, {"password": "x"})
    assert cfg == {"host": "192.0.2.10", "port": 3494, "ups": "office"} and sec == {}
    cfg, _ = core_mod.Core.clean_integration("nut", {"host": "192.0.2.10"}, {})
    assert cfg == {"host": "192.0.2.10", "port": 3493}                            # default port, no UPS name: every UPS the server offers


@pytest.mark.parametrize("config,err", [({}, "integration.err.host"), ({"host": "bad host"}, "integration.err.host"), ({"host": "h" * 300}, "integration.err.host"),
                                        ({"host": "h", "port": "abc"}, "integration.err.port"), ({"host": "h", "port": 70000}, "integration.err.port"),
                                        ({"host": "h", "port": -1}, "integration.err.port"), ({"host": "h", "ups": "bad name"}, "integration.err.value"),
                                        ({"host": "h", "ups": "a;b"}, "integration.err.value")])
def test_nut_settings_are_refused(config, err):
    with pytest.raises(ValueError, match=err):
        core_mod.Core.clean_integration("nut", config, {})


def test_nut_has_no_secret_fields_and_a_source_of_its_own():
    assert core_mod.integration_fields("nut") == (("host", "port", "ups"), ())
    from lanloom import sources
    assert sources.SOURCES["nut"] is sources.Nut
    c_src = core_mod.Core._build_source(SimpleNamespace(), "nut", {"host": "192.0.2.10", "port": 3493, "ups": ""}, {})
    assert repr(c_src) == "Nut(192.0.2.10:3493)"


# ---------------------------------------------------------------------------------------------------- events
def nut_event(host="192.168.50.10", status="OL", name="office", t=T):
    return {"type": "nut", "source": "nut", "host": host, "t": t, "units": [{"name": name, "description": "Rack UPS", "status": status, "charge": 80.0,
                                                                          "runtime_s": 1500.0}]}


def test_a_nut_event_reaches_the_ups_book_and_the_cadence(tmp_path):
    c = make(tmp_path)
    c.on_event("int:3", 3, nut_event())
    assert list(c.controlroom.ups.units) == ["nut:3:office"] and "int:3" in c.cadence and c.cadence["int:3"].last
    c.on_event("int:3", 3, nut_event(status="OB"))
    assert c.controlroom.ups.units["nut:3:office"]["state"] == "battery"


def test_a_status_event_does_not_count_as_a_reading(tmp_path):
    c = make(tmp_path)
    c.on_event("int:3", 3, {"type": "status", "source": "nut", "ok": False, "key": "source.nut.refused", "t": T})
    assert c.source_status["int:3"]["key"] == "source.nut.refused" and c.controlroom.ups.units == {}


def test_snmp_frame_with_a_ups_mib_reaches_the_ups_book(tmp_path):
    c = make(tmp_path)
    frame = {"type": "snmp", "source": "snmp", "host": "192.168.50.2", "t": T, "sys": {"name": "sw"}, "ifaces": [],
             "ups": {"name": "snmp-ups", "status": "OB", "charge": 50.0, "runtime_s": 600.0}}
    c.on_event("int:4", 4, frame)
    assert c.controlroom.ups.units["snmp:4:snmp-ups"]["state"] == "battery"
    c.on_event("int:4", 4, dict(frame, ups=None))
    assert list(c.controlroom.ups.units) == ["snmp:4:snmp-ups"]


def test_removing_a_connection_forgets_its_ups(tmp_path):
    c = make(tmp_path)
    integ = c.save_integration("nut", "Battery backup", {"host": "192.0.2.10"}, {})
    c.on_event("int:%d" % integ["id"], integ["id"], nut_event())
    assert len(c.controlroom.ups.units) == 1
    c.delete_integration(integ["id"])
    assert c.controlroom.ups.units == {}


def test_gateway_events_of_a_family_state_are_booked_once(tmp_path):
    from lanloom.firewall import neutral
    c = make(tmp_path)
    integ = c.save_integration("nut", "Stand-in", {"host": "192.0.2.10"}, {})            # any stored connection id will do
    st = {"devices": [], "networks": [], "wans": [], "wan_events": [{"t": T - 100, "kind": "down", "line": "Fibre"}, {"t": T - 50, "kind": "up", "line": "Fibre"}]}
    neutral.validate(st)
    from lanloom.demo_live import gateway_capabilities
    ev = {"type": "config", "family": "unifi", "state": st, "capabilities": gateway_capabilities("unifi")}
    c._book_config(integ["id"], ev, T)
    c._book_config(integ["id"], ev, T + 60)
    rows = c.store.query("SELECT kind, line FROM wan_log ORDER BY t")
    assert [(r["kind"], r["line"]) for r in rows] == [("down", "Fibre"), ("up", "Fibre")]


def test_a_flood_of_gateway_events_is_cut_off(tmp_path):
    c = make(tmp_path)
    events = [{"t": float(i), "kind": "down", "line": "L"} for i in range(1000)]
    assert c.controlroom.wan.ingest_gateway_events(events) == 200
    assert c.controlroom.wan.ingest_gateway_events({"t": 1.0, "kind": "down"}) == 0          # not a list
    assert c.controlroom.wan.ingest_gateway_events("down") == 0


def test_family_state_with_junk_in_the_new_lists_is_refused(tmp_path):
    from lanloom.firewall import neutral
    for key in ("wans", "cellular"):
        with pytest.raises(neutral.ModelError):
            neutral.validate({"devices": [], "networks": [], key: [{"name": "no id"}]})
        with pytest.raises(neutral.ModelError):
            neutral.validate({"devices": [], "networks": [], key: [{"id": "a"}, {"id": "a"}]})
        with pytest.raises(neutral.ModelError):
            neutral.validate({"devices": [], "networks": [], key: [{"id": "02:4c:4c:00:00:01"}]})
    neutral.validate({"devices": [], "networks": [], "wans": [{"id": "n1"}], "cellular": [{"id": "c1"}], "wan_events": [], "ids": {"enabled": True}})


# ---------------------------------------------------------------------------------------------------- sample integrations
def test_the_demo_integration_list_has_the_battery_backup_connection(tmp_path):
    from lanloom.demo import Demo
    c = make(tmp_path, demo=True)
    d = Demo()
    c.demo = d
    d.seed(c)
    d.seed_integrations(c)
    kinds = [i["kind"] for i in c.integrations()]
    assert kinds.count("nut") == 1 and kinds.count("snmp") >= 1
    nut = next(i for i in c.integrations() if i["kind"] == "nut")
    assert nut["config"] == {"host": "192.168.50.10", "port": 3493}
