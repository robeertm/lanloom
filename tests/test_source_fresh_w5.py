"""W5: a hub's radio devices count as "ok" only while the hub connection is fresh. Fixed clock, no sleeping."""
import datetime

from lanloom import core as core_mod, hubbridge as hb, hubmerge as hm, live, portfront
from lanloom.nightly import render
from test_hub_bridge_core import NOW, SINCE, UNTIL, add_hub, make_core
from test_hub_merge import ent, hubdev, snap


def iso(age_s):
    return datetime.datetime.fromtimestamp(NOW - age_s, datetime.timezone.utc).isoformat(timespec="seconds")


def hub_snap():
    devices = [hubdev("c1", "Mesh stick", radio="mesh154", role="coordinator"),
               hubdev("a", "Plug", radio="mesh154", via="c1"),
               hubdev("b", "Sensor", radio="mesh154", via="c1"),
               hubdev("px", "Proxy", radio="ble", role="receiver")]
    ents = [ent("sensor.stick", "c1", "online"), ent("light.a", "a", "on"),
            ent("sensor.b_bat", "b", "4", dc="battery", domain="sensor"), ent("sensor.px", "px", "on")]
    return snap(devices, ents)


# ---------------------------------------------------------------------- the rule
def test_source_live_rule():
    assert hm.source_live(True, NOW - 100, 10, NOW)                      # younger than the floor of 120 s
    assert not hm.source_live(True, NOW - 130, 10, NOW)
    assert hm.source_live(True, NOW - 200, 60, NOW)                      # 4 x 60 s = 240 s
    assert not hm.source_live(True, NOW - 250, 60, NOW)
    assert not hm.source_live(False, NOW - 100, 10, NOW)                 # a failed connection is not fresh
    assert hm.source_live(False, NOW - 10, 10, NOW)                      # a failure just now: the last event still counts
    assert hm.source_live(None, None, None, NOW)                         # no information: nothing to doubt


# ---------------------------------------------------------------------- pure views
def test_fresh_hub_is_as_before():
    v = hm.radio_view(hub_snap(), {}, NOW, True)
    assert v["live"] and v["state"]["a"]["fresh"] == "ok" and v["state"]["b"]["battery"] == 4
    site = hm.onsite(hub_snap(), {}, v, NOW)
    assert site["counts"]["now"] == 1 and site["counts"]["unknown"] == 0


def test_stale_hub_shows_no_current_data_never_down_or_battery():
    s = hub_snap()
    v = hm.radio_view(s, {}, NOW, False)
    for did in ("a", "b", "px", "c1"):
        st = v["state"][did]
        assert st["fresh"] == "unknown" and st["stale"] and st["battery"] is None and not st["battery_low"] and st["grade"] == "unknown"
    assert all(n["health"] == "unknown" and n["silent"] == 0 and n["battery"] == 0 for n in v["networks"])
    site = hm.onsite(s, {}, v, NOW)
    assert site["counts"]["silent"] == 0 and site["counts"]["now"] == 0 and site["counts"]["ok"] == 0
    assert site["counts"]["unknown"] == len(site["items"]) and all(i["cat"] == "unknown" and i["battery"] is None for i in site["items"])
    h = hm.health(s, v)
    assert not h["live"] and h["receivers"][0]["fresh"] == "unknown" and h["ipstd"]["silent"] == 0


def test_stale_hub_keeps_no_silent_verdict_even_for_unavailable_devices():
    s = snap([hubdev("a", "Plug", radio="mesh154")], [ent("light.a", "a", "unavailable")])
    assert hm.radio_view(s, {}, NOW, True)["state"]["a"]["fresh"] == "silent"
    assert hm.radio_view(s, {}, NOW, False)["state"]["a"]["fresh"] == "unknown"


def test_entity_with_own_last_seen_is_unchanged_while_fresh():
    s = snap([hubdev("a", "Plug", radio="mesh154")], [ent("sensor.ls", "a", iso(30), ls=True)])
    st = hm.radio_view(s, {}, NOW, True)["state"]["a"]
    assert st["fresh"] == "ok" and st["last_seen_s"] == 30


# ---------------------------------------------------------------------- core
def fed(tmp_path, last_age, ok=True):
    c, rep = make_core(tmp_path)
    iid = add_hub(c)
    entry = c.hub[iid] = hb.new_entry(None)
    hb.book(entry, dict({"type": "hub_snapshot", "t": NOW, "areas": [], "trackers": [], "automations": [], "scripts": []},
                        devices=hub_snap()["devices"], entities=hub_snap()["entities"]), NOW)
    key = "int:%d" % iid
    cad = c.cadence[key] = live.Cadence()
    for k in (3, 2, 1, 0):
        cad.tick(NOW - last_age - 60 * k)
    c.source_status[key] = {"ok": ok, "key": "source.ok", "t": NOW}
    return c, rep, iid


def test_core_hub_live_and_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(core_mod.time, "time", lambda: NOW)
    c, _, iid = fed(tmp_path, 30)
    assert c.hub_live(iid, NOW) and c.hub_radio(iid, NOW)["state"]["a"]["fresh"] == "ok"
    c, _, iid = fed(tmp_path / "x", 600)
    assert not c.hub_live(iid, NOW)
    assert c.hub_radio(iid, NOW)["state"]["a"]["fresh"] == "unknown"
    ctx = c.hub_sensors(iid)
    assert ctx["connected"] is False and ctx["live"] is False and ctx["devices"]       # the snapshot stays visible


def test_core_connection_back_is_fresh_at_once(tmp_path):
    c, _, iid = fed(tmp_path, 600)
    assert c.hub_radio(iid, NOW)["state"]["a"]["fresh"] == "unknown"
    c.cadence["int:%d" % iid].tick(NOW)
    assert c.hub_radio(iid, NOW + 1)["state"]["a"]["fresh"] == "ok"


def test_core_failed_connection_is_not_fresh(tmp_path):
    c, _, iid = fed(tmp_path, 60, ok=False)
    assert not c.hub_live(iid, NOW)


def test_nightly_stale_source_is_one_point_not_many(tmp_path):
    c, rep, iid = fed(tmp_path, 600)
    secs = [s for s in rep.assemble(SINCE, UNTIL, None, "2026-11-16", [])["sections"] if s["id"] == "hub_radio"]
    (sec,) = secs
    assert [i["key"] for i in sec["items"]] == ["report.radio.source_stale"] and sec["items"][0]["point"]


# ---------------------------------------------------------------------- the other adapters
def test_roam_signal_is_not_shown_after_the_controller_went_quiet():
    r = portfront.RoamWatch()
    cl = [{"id": "c1", "ap": "ap1", "band": "5", "signal": -85, "name": "Phone"}]
    for k in range(66):
        r.feed(cl, NOW + 10 * k)
    t = NOW + 655
    assert r.signal_view("ap1", True, t)["weakest"] and r.stuck({"ap1": "A", "ap2": "B"}, t)
    late = t + 10_000
    assert not r.live(late) and r.signal_view("ap1", True, late) == {} and r.stuck({"ap1": "A", "ap2": "B"}, late) == []
    r.feed(cl, late)
    assert r.live(late + 1) and r.signal_view("ap1", True, late + 1)
