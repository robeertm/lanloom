"""H6: effect chains (sensor -> automation -> script -> device) and mode conflicts, both read from the one summary of stored
runs the bridge reports - nothing is asked per automation.

Fixed clock, sample data; the hub is a fake. Covers the bridge side (context ids resolved to ``by`` inside the bridge,
private triggers dropped), the core views (one hour window, loops, depth, modes), the nightly section, the API and the
admin-mode rule (without it nothing is asked)."""
import json
import time

import pytest

from lanloom import demo_hub, hub_privacy as hp, hubauto, hubbridge as hb, hubchain
from lanloom.nightly import autosection
from lanloom.sources import hub as hub_src
from test_hub_auto import CONFIGS, api, entry, demo_entry  # noqa: F401  (api: fixture)
from test_hub_bridge_core import NOW, add_hub, make_core

DAY = 86400


def run(at, r="ok", trig=None, by=None, end=None):
    return {"at": at, "end": end if end is not None else at + 5, "r": r, "trig": trig, "by": by}


def with_runs(runs, changes=(), t=NOW - 30):
    """The sample entry with a run summary and some extra state changes (time, entity, state, cause)."""
    e = entry()
    for when, eid, st, by in changes:
        ev = {"type": "hub_state", "entity_id": eid, "state": st, "t": when}
        if by:
            ev["by"] = by
        hb.book(e, ev, NOW)
    hb.book(e, {"type": "hub_runs", "t": t, "items": {}, "runs": runs, "checked": 9}, NOW)
    return e


CHAIN_RUNS = {"automation.hall_motion": [run(NOW - 520, trig="binary_sensor.hall_motion")],
              "script.night": [run(NOW - 510, by="automation.hall_motion")]}
CHAIN_CHANGE = [(NOW - 500, "switch.desk", "off", "script.night")]


def chains_of(e, **kw):
    return hubchain.chains(hubauto.Ctx(e, NOW), **kw)


# ---------------------------------------------------------------------- 1. the bridge side
def _bridge_hub():
    h = hub_src.Hub("http://192.0.2.5:8123", "t" * 40, admin_mode=True)
    h._items = {"automation.a": {"id": "1"}, "automation.b": {"id": "2"}, "script.s": {"id": "s"}}
    h._states = {"binary_sensor.motion": ("off", 0, "Motion"), "person.alice": ("home", 0, "Alice"), "light.lamp": ("on", 0, "Lamp")}
    return h


def _raw(item, domain, start, ctx_id, parent=None, trigger=None, execution="finished", error=None):
    r = {"domain": domain, "item_id": item, "run_id": "r" + ctx_id, "script_execution": execution,
         "timestamp": {"start": "2026-11-16T07:00:%02d+00:00" % start, "finish": "2026-11-16T07:00:%02d+00:00" % (start + 1)},
         "context": {"id": ctx_id, "parent_id": parent, "user_id": "u-secret"}}
    if trigger:
        r["trigger"] = trigger
    if error:
        r["error"] = error
    return r


def test_bridge_resolves_context_ids_to_the_calling_item_and_drops_the_rest():
    h = _bridge_hub()
    job = {"todo": [], "items": {}, "runs": {}, "checked": 0, "error": None}
    h._sum_runs(job, "automation", [
        _raw("1", "automation", 1, "ctxA", trigger="state of binary_sensor.motion"),
        _raw("2", "automation", 5, "ctxB", trigger="state of person.alice"),                 # a private trigger: no entity
        _raw("1", "automation", 9, "ctxC", execution="failed_single", trigger="time"),
        _raw("2", "automation", 12, "ctxD", execution="failed_max_runs"),
        _raw("1", "automation", 15, "ctxE", execution="cancelled", trigger="state of 1.5 light.lamp")])
    h._sum_runs(job, "script", [_raw("s", "script", 2, "ctxS", parent="ctxA", trigger="whatever 192.168.1.5"),
                                _raw("zz", "script", 3, "ctxZ")])                              # an item the bridge does not follow
    got = h._resolve_runs(job["runs"])
    blob = json.dumps(got)
    assert "ctx" not in blob and "u-secret" not in blob and "alice" not in blob and "192.168" not in blob
    assert [r["r"] for r in got["automation.a"]] == ["cancelled", "single", "ok"]              # newest first
    assert got["automation.a"][0]["trig"] == "light.lamp" and got["automation.a"][2]["trig"] == "binary_sensor.motion"
    assert got["automation.a"][1]["trig"] is None                                              # "time" names no entity
    assert got["automation.b"][1]["trig"] is None and got["automation.b"][0]["r"] == "max_runs"
    assert got["script.s"] == [{"at": got["script.s"][0]["at"], "end": got["script.s"][0]["end"], "r": "ok", "trig": None,
                                "by": "automation.a"}]
    assert "script.zz" not in got and job["checked"] == 6


def test_bridge_keeps_only_the_newest_runs_and_never_a_self_call():
    h = _bridge_hub()
    job = {"todo": [], "items": {}, "runs": {}, "checked": 0, "error": None}
    h._sum_runs(job, "automation", [_raw("1", "automation", i, "c%d" % i, parent="c%d" % i if i == 3 else None) for i in range(30)])
    got = h._resolve_runs(job["runs"])["automation.a"]
    assert len(got) == hub_src.MAX_KEPT_RUNS and got[0]["at"] > got[-1]["at"]
    assert all(r["by"] is None for r in got)                                                   # a run is not its own caller


def test_core_cleans_a_summary_again_and_ignores_garbage():
    ev = {"t": NOW, "runs": {"automation.a": [run(NOW - 5, trig="person.alice", by="light.not_an_item"), {"at": "x"}, "junk",
                                                run(NOW - 9, r="weird", by="automation.a")],
                             "light.x": [run(NOW)], "person.p": [run(NOW)], "script.s": "junk"},
          "items": {}, "checked": 1}
    got = hb.clean_run_summary(ev, NOW)["runs"]
    assert list(got) == ["automation.a"]
    assert got["automation.a"][0]["trig"] is None and got["automation.a"][0]["by"] is None     # private trigger, not an item
    assert got["automation.a"][1]["r"] == "other" and got["automation.a"][1]["by"] is None    # an unknown word; self call dropped
    assert hb.clean_run_summary({"t": NOW}, NOW)["runs"] == {}


# ---------------------------------------------------------------------- 2. effect chains
def test_a_chain_runs_from_the_sensor_through_automation_and_script_to_the_device():
    e = with_runs(CHAIN_RUNS, CHAIN_CHANGE)
    got = chains_of(e)
    assert [c["entity"] for c in got] == ["switch.desk"]
    c = got[0]
    assert [n["id"] for n in c["links"]] == ["binary_sensor.hall_motion", "automation.hall_motion", "script.night", "switch.desk"]
    assert [n["kind"] for n in c["links"]] == ["entity", "automation", "script", "entity"]
    assert [n["name"] for n in c["links"]][:3] == ["Hall Motion", "Hall motion", "Night"]       # display names, not ids
    assert c["origin"] and not c["cut"] and not c["cycle"] and c["depth"] == 2
    assert c["links"][-1]["state"] == "off" and c["links"][-1]["at"] == NOW - 500


def test_a_run_older_than_an_hour_before_the_switch_does_not_count():
    old = {"automation.hall_motion": [run(NOW - 500 - 3601, trig="binary_sensor.hall_motion")],
           "script.night": [run(NOW - 510, by="automation.hall_motion")]}
    c = chains_of(with_runs(old, CHAIN_CHANGE))[0]
    assert [n["id"] for n in c["links"]] == ["automation.hall_motion", "script.night", "switch.desk"]   # the automation's run is missing
    assert not c["origin"] and c["links"][0]["at"] is None                                         # shown, but without a run and a start
    edge = {"automation.hall_motion": [run(NOW - 500 - 3600, trig="binary_sensor.hall_motion")],
            "script.night": [run(NOW - 3000, by="automation.hall_motion")]}
    assert chains_of(with_runs(edge, CHAIN_CHANGE))[0]["origin"]                                    # exactly one hour counts
    gone = {"script.night": [run(NOW - 500 - 3700, by="automation.hall_motion")]}
    assert chains_of(with_runs(gone, CHAIN_CHANGE)) == []                                           # nothing upstream is known: no chain


def test_a_calling_run_must_not_start_after_the_run_it_called():
    late = {"automation.hall_motion": [run(NOW - 400, trig="binary_sensor.hall_motion")],         # started after the script run
            "script.night": [run(NOW - 510, by="automation.hall_motion")]}
    c = chains_of(with_runs(late, CHAIN_CHANGE))
    assert c == [] or not c[0]["origin"]


def test_the_newest_run_before_the_change_is_the_one_used():
    runs = {"automation.hall_motion": [run(NOW - 100, trig="binary_sensor.desk_motion"), run(NOW - 520, trig="binary_sensor.hall_motion")],
            "script.night": [run(NOW - 510, by="automation.hall_motion")]}
    c = chains_of(with_runs(runs, CHAIN_CHANGE))[0]
    assert c["links"][0]["id"] == "binary_sensor.hall_motion"                                       # the run at -100 is after the change


def test_a_loop_of_callers_ends_the_chain():
    runs = {"automation.hall_motion": [run(NOW - 520, by="script.night")],
            "script.night": [run(NOW - 510, by="automation.hall_motion")]}
    c = chains_of(with_runs(runs, CHAIN_CHANGE))[0]
    assert c["cycle"] and not c["origin"]
    assert [n["id"] for n in c["links"]] == ["automation.hall_motion", "script.night", "switch.desk"]


def test_a_chain_has_at_most_four_automations_or_scripts():
    names = ["automation.desk_on", "automation.desk_off", "automation.hall_motion", "automation.echo", "automation.dawn", "script.night"]
    runs = {names[i]: [run(NOW - 700 + i * 10, by=names[i + 1] if i + 1 < len(names) else None, trig="binary_sensor.hall_motion")]
            for i in range(len(names))}
    # the device was switched by names[0]; names[0] was called by names[1] ... a chain of six: cut after four
    runs = {names[i]: [run(NOW - 700 + (len(names) - i) * 10, by=names[i + 1] if i + 1 < len(names) else None, trig="binary_sensor.hall_motion")]
            for i in range(len(names))}
    c = chains_of(with_runs(runs, [(NOW - 500, "switch.desk", "off", names[0])]))[0]
    autos = [n for n in c["links"] if n["kind"] in ("automation", "script")]
    assert len(autos) == hubchain.MAX_DEPTH == 4 and c["cut"] and not c["origin"]
    three = {k: v for k, v in runs.items()}
    three[names[2]] = [run(NOW - 650, trig="binary_sensor.hall_motion")]                             # ends after three
    c3 = chains_of(with_runs(three, [(NOW - 500, "switch.desk", "off", names[0])]))[0]
    assert len([n for n in c3["links"] if n["kind"] in ("automation", "script")]) == 3 and c3["origin"] and not c3["cut"]


def test_filters_by_device_entity_and_item_and_per_entity_limit():
    e = with_runs(CHAIN_RUNS, CHAIN_CHANGE)
    assert [c["entity"] for c in chains_of(e, entities=["switch.desk"])] == ["switch.desk"]
    assert chains_of(e, entities=["light.hall"]) == []
    assert len(chains_of(e, item="script.night")) == 1 and chains_of(e, item="automation.echo") == []
    c = hubauto.chain_view(hubauto.Ctx(e, NOW), device="d2")                                          # the desk plug's hub device
    assert [x["entity"] for x in c["chains"]] == ["switch.desk"] and c["runs_summary"]["state"] == "ok"
    assert hubauto.chain_view(hubauto.Ctx(e, NOW), entity="switch.desk", item="script.night")["chains"]


def test_an_older_change_is_used_when_the_newest_one_has_no_run_behind_it():
    changes = CHAIN_CHANGE + [(NOW - 20, "switch.desk", "on", "script.night")]                      # a newer switch with no run in its hour
    e = with_runs({"automation.hall_motion": [run(NOW - 520, trig="binary_sensor.hall_motion")],
                   "script.night": [run(NOW - 510, by="automation.hall_motion"), run(NOW - 30, by=None)]}, changes)
    got = chains_of(e)
    assert got and got[0]["t"] in (NOW - 500, NOW - 20)


def test_summary_states_and_no_chain_without_a_summary():
    e = entry()
    assert hubchain.summary(e, NOW)["state"] == "waiting" and chains_of(e) == []
    e["run_status"] = {"t": NOW, "key": "timeout"}
    assert hubchain.summary(e, NOW) == {"state": "error", "t": None, "age_s": None, "error": "timeout"}
    e2 = with_runs(CHAIN_RUNS, t=NOW - 2 * 3600)
    s = hubchain.summary(e2, NOW)
    assert s["state"] == "ok" and s["stale"] and s["age_s"] == 7200.0
    assert hubchain.summary(hb.new_entry(None), NOW)["state"] == "no_hub"
    e3 = entry()
    e3["snapshot"]["admin"] = False
    assert hubchain.summary(e3, NOW)["state"] == "admin_off"


# ---------------------------------------------------------------------- 3. mode conflicts
def ctx_with(runs, modes=None, t=NOW - 30):
    e = with_runs(runs, t=t)
    for it in e["snapshot"]["automations"] + e["snapshot"]["scripts"]:
        if modes and it["entity_id"] in modes:
            it["mode"] = modes[it["entity_id"]]
    return hubauto.Ctx(e, NOW)


def events(c, since=None, until=None):
    return hubchain.mode_events(c, NOW - DAY if since is None else since, NOW if until is None else until)


def test_single_drops_restart_from_three_in_an_hour_and_full_queues_are_found():
    runs = {"automation.hall_motion": [run(NOW - 300, r="single", end=NOW - 300), run(NOW - 900, r="single", end=NOW - 900), run(NOW - 1200, r="ok")],
            "automation.desk_on": [run(NOW - 3000, r="cancelled"), run(NOW - 2400, r="cancelled"), run(NOW - 1800, r="cancelled")],
            "automation.echo": [run(NOW - 700, r="max_runs", end=NOW - 700)]}
    got = {(x["entity"], x["kind"]): x for x in events(ctx_with(runs))}
    assert got[("automation.hall_motion", "single")]["n"] == 2 and got[("automation.hall_motion", "single")]["last"] == NOW - 300
    assert got[("automation.desk_on", "restart")]["n"] == 3 and got[("automation.desk_on", "restart")]["mode"] == "restart"
    assert got[("automation.echo", "max_runs")]["mode"] == "parallel" and got[("automation.echo", "max_runs")]["name"] == "Echo"
    assert len(got) == 3


def test_two_restarts_or_restarts_spread_over_hours_are_no_conflict():
    two = {"automation.desk_on": [run(NOW - 3000, r="cancelled"), run(NOW - 2400, r="cancelled")]}
    assert events(ctx_with(two)) == []
    spread = {"automation.desk_on": [run(NOW - 3000 - i * 4000, r="cancelled") for i in range(4)]}
    assert events(ctx_with(spread)) == []
    # a stop in an automation that is not in restart mode was somebody stopping it, not the mode
    other = {"automation.desk_off": [run(NOW - 100, r="cancelled"), run(NOW - 200, r="cancelled"), run(NOW - 300, r="cancelled")]}
    assert events(ctx_with(other)) == []
    wrong = {"automation.desk_off": [run(NOW - 100, r="max_runs")]}                                    # mode single cannot hit a maximum
    assert events(ctx_with(wrong)) == []
    unknown = {"automation.desk_off": [run(NOW - 100, r="max_runs")]}
    assert events(ctx_with(unknown, {"automation.desk_off": "queued"}))[0]["kind"] == "max_runs"


def test_only_runs_inside_the_window_count():
    runs = {"automation.hall_motion": [run(NOW - 100, r="single", end=NOW - 100), run(NOW - 3 * DAY, r="single", end=NOW - 3 * DAY)]}
    c = ctx_with(runs)
    assert events(c)[0]["n"] == 1
    assert events(c, since=NOW - 50) == [] and events(c, since=NOW - 200, until=NOW - 150) == []


def test_the_live_finding_and_filter_counts_carry_the_mode_conflict():
    runs = {"automation.hall_motion": [run(NOW - 300, r="single", end=NOW - 300)]}
    e = with_runs(runs)
    c = hubauto.Ctx(e, NOW)
    rows = {r["id"]: r for r in hubauto.rows(c)}
    f = [x for x in rows["automation.hall_motion"]["findings"] if x["code"] == "mode"]
    assert f == [{"code": "mode", "kind": "single", "mode": "single", "n": 1, "last": NOW - 300, "level": "warn"}]
    assert hubauto.tab(hubauto.Ctx(e, NOW))["counts"]["mode"] == 1
    assert hubauto.detail(hubauto.Ctx(e, NOW), "automation.hall_motion")["runs_summary"]["state"] == "ok"


def test_without_admin_mode_nothing_is_ever_asked_and_the_views_say_so():
    e = entry()
    e["snapshot"]["admin"] = False
    c = hubauto.Ctx(e, NOW)
    assert hubchain.summary(e, NOW)["state"] == "admin_off" and hubchain.chains(c) == [] and hubchain.mode_events(c, 0, NOW) == []
    assert hubauto.chain_view(c)["runs_summary"]["state"] == "admin_off"


# ---------------------------------------------------------------------- 4. the nightly report
def night(rep, e_runs):
    return rep.assemble(NOW - DAY, NOW, None, "2026-11-16", [])


def sample_core(tmp_path, runs):
    c, rep = make_core(tmp_path)
    iid = add_hub(c)
    c.store.execute("UPDATE integrations SET config=? WHERE id=?", (json.dumps({"url": "http://192.0.2.5:8123", "admin_mode": True}), iid))
    e = c.hub[iid] = with_runs(runs, t=NOW)
    e["run_status"] = {"t": NOW, "key": "ok"}
    return c, rep, iid, e


def test_the_nightly_report_names_each_mode_conflict_with_count_time_and_a_link(tmp_path):
    runs = {"automation.hall_motion": [run(NOW - 300, r="single", end=NOW - 300), run(NOW - 900, r="single", end=NOW - 900)],
            "automation.desk_on": [run(NOW - 3000, r="cancelled"), run(NOW - 2400, r="cancelled"), run(NOW - 1800, r="cancelled")],
            "automation.echo": [run(NOW - 700, r="max_runs", end=NOW - 700)]}
    c, rep, iid, e = sample_core(tmp_path, runs)
    (sec,) = [s for s in rep.assemble(NOW - DAY, NOW, None, "2026-11-16", [])["sections"] if s["id"] == "auto"]
    by_key = {i["key"]: i for i in sec["items"] if i["key"].startswith("report.auto.mode_")}
    assert set(by_key) == {"report.auto.mode_single", "report.auto.mode_restart", "report.auto.mode_max_runs"}
    single = by_key["report.auto.mode_single"]
    assert single["point"] and single["level"] == "warn" and single["vars"] == {"name": "Hall motion", "n": 2, "last_at": NOW - 300}
    assert single["href"] == "#/automations/%d/item/automation.hall_motion" % iid
    assert by_key["report.auto.mode_restart"]["vars"]["n"] == 3
    assert {"key": "report.auto.part.mode_conflicts", "vars": {"n": 3}} in sec["summary"]["parts"]
    blob = json.dumps({k: v["vars"] for k, v in by_key.items()})
    assert "automation." not in blob and "binary_sensor." not in blob                              # names only; ids stay in the link


def test_mode_conflicts_stay_points_every_night_and_are_never_in_the_report_without_a_fresh_summary(tmp_path):
    runs = {"automation.hall_motion": [run(NOW - 300, r="single", end=NOW - 300)]}
    c, rep, iid, e = sample_core(tmp_path, runs)
    r0 = rep.assemble(NOW - DAY, NOW, None, "2026-11-16", [])
    again = rep.assemble(NOW - DAY, NOW, r0, "2026-11-17", [])
    for r in (r0, again):
        (sec,) = [s for s in r["sections"] if s["id"] == "auto"]
        assert [i["key"] for i in sec["items"] if i["key"] == "report.auto.mode_single" and i["point"]] == ["report.auto.mode_single"]
    e["run_summary"]["t"] -= 3 * 3600                                                              # read three hours before the report: stale
    (stale,) = [s for s in rep.assemble(NOW - DAY, NOW, r0, "2026-11-17", [])["sections"] if s["id"] == "auto"]
    assert not [i for i in stale["items"] if i["key"].startswith("report.auto.mode_")]


def test_more_than_five_mode_conflicts_are_counted_in_one_point(tmp_path):
    names = ["automation.desk_on", "automation.desk_off", "automation.hall_motion", "automation.ghost", "automation.echo", "automation.dawn"]
    runs = {n: [run(NOW - 300, r="single", end=NOW - 300)] for n in names}
    c, rep, iid, e = sample_core(tmp_path, runs)
    (sec,) = [s for s in rep.assemble(NOW - DAY, NOW, None, "2026-11-16", [])["sections"] if s["id"] == "auto"]
    named = [i for i in sec["items"] if i["key"] == "report.auto.mode_single"]
    (more,) = [i for i in sec["items"] if i["key"] == "report.auto.mode_more"]
    assert len(named) == autosection.MAX_MODE_NAMED == 5 and more["vars"]["n"] == 1


def test_two_sites_do_not_mix_their_runs(tmp_path):
    c, rep = make_core(tmp_path)
    a, b = add_hub(c, "Site A"), add_hub(c, "Site B")
    for iid in (a, b):
        c.store.execute("UPDATE integrations SET config=? WHERE id=?", (json.dumps({"url": "http://192.0.2.5:8123", "admin_mode": True}), iid))
    c.hub[a] = with_runs({"automation.hall_motion": [run(NOW - 300, r="single", end=NOW - 300)]}, t=NOW)
    c.hub[b] = with_runs({}, t=NOW)
    for iid in (a, b):
        c.hub[iid]["run_status"] = {"t": NOW, "key": "ok"}
    secs = [s for s in rep.assemble(NOW - DAY, NOW, None, "2026-11-16", [])["sections"] if s["id"] == "auto"]
    assert len(secs) == 2
    with_mode = [s for s in secs if any(i["key"] == "report.auto.mode_single" for i in s["items"])]
    assert len(with_mode) == 1 and secs.index(with_mode[0]) == 0        # Site A (added first) has it, Site B has none
    assert c.hub_chain(a)["chains"] == [] and c.hub_chain(99) is None


# ---------------------------------------------------------------------- 5. the API and the one summary request
def test_api_chain_for_a_viewer_returns_chains_and_validates_ids(api):
    api.cookies.clear()
    assert api.post("/api/login", json={"username": "viewer1", "password": "a-long-test-password"}, headers=api.hdr).status_code == 200
    hid = api.hub_id
    app_core = api.app.state.ll.core
    app_core.hub[hid] = with_runs(CHAIN_RUNS, CHAIN_CHANGE, t=time.time())
    now = time.time()
    # the sample entry is dated NOW (2027): shift the clock-sensitive parts by asking the view itself
    r = api.get("/api/hub/%d/auto/chain" % hid)
    assert r.status_code == 200 and {"chains", "runs_summary", "window_s", "max_depth"} <= set(r.json())
    assert api.get("/api/hub/%d/auto/chain?entity=not%%20an%%20id" % hid).status_code == 400
    assert api.get("/api/hub/%d/auto/chain?item=bad" % hid).status_code == 400
    assert api.get("/api/hub/999/auto/chain").status_code == 404
    assert api.get("/api/hub/%d/auto/chain?device=d2&item=script.night" % hid).status_code == 200
    api.cookies.clear()
    assert api.get("/api/hub/%d/auto/chain" % hid).status_code == 401
    assert now


class _Src:
    def __init__(self):
        self.asked = 0

    def want_run_summary(self):
        self.asked += 1
        return True


def test_the_summary_is_asked_once_for_all_views_never_per_automation_and_never_without_admin_mode(tmp_path):
    c, rep = make_core(tmp_path)
    iid = add_hub(c)
    c.hub[iid] = entry()
    src = _Src()
    c._hub_sources = {iid: src}
    assert c.hub_refresh_runs(iid, NOW) == "admin_off" and src.asked == 0                           # no admin mode on the connection
    c.store.execute("UPDATE integrations SET config=? WHERE id=?", (json.dumps({"url": "http://192.0.2.5:8123", "admin_mode": True}), iid))
    assert c.hub_refresh_runs(iid, NOW) == "asked" and src.asked == 1
    for k in range(10):                                                                             # ten views within five minutes: no new request
        assert c.hub_refresh_runs(iid, NOW + 10 + k) == "fresh"
    assert src.asked == 1
    assert c.hub_refresh_runs(iid, NOW + hb.RUNS_REFRESH_S + 1) == "asked" and src.asked == 2
    c.hub[iid]["snapshot"]["admin"] = False
    assert c.hub_refresh_runs(99, NOW) == "offline"


def test_the_bridge_asks_the_hub_with_two_lists_and_no_new_message_type():
    # the summary is the same two trace/list requests as before (see test_sources_hub_admin); H6 adds no message type
    assert "trace/list" in hub_src.ALLOWED_TYPES and "trace/get" in hub_src.ALLOWED_TYPES
    assert hub_src.ALLOWED_TYPES == hub_src.ALLOWED_TYPES | {"trace/list"}
    job = _bridge_hub()
    job.want_run_summary()
    assert job._runs_job["todo"] == ["automation", "script"] and job._runs_job["runs"] == {}


# ---------------------------------------------------------------------- 6. the demo and the budget
def test_the_demo_has_the_chain_and_the_three_mode_conflicts():
    e = demo_entry()
    c = hubauto.Ctx(e, NOW)
    got = hubchain.chains(c, entities=["light.hallway_2"])
    assert got, "the sample chain: motion sensor -> light automation -> scene script -> lamp"
    four = [c for c in got if len(c["links"]) == 4]
    assert four, "the lamp switched on by the scene: four links"
    chain = [n["name"] for n in four[0]["links"]]
    assert chain[:3] == ["Hallway motion", "Hallway light on motion", "Hallway scene"] and four[0]["links"][-1]["state"] == "on"
    assert any(len(c["links"]) == 3 for c in got)                                                    # and switched off by the automation itself
    kinds = {(x["entity"], x["kind"]) for x in hubchain.mode_events(c, NOW - DAY, NOW)}
    assert {("automation.garden_light_on_motion", "single"), ("automation.desk_focus", "restart"),
            ("automation.kitchen_climate", "max_runs")} <= kinds
    assert hubchain.summary(e, NOW)["state"] == "ok"


def test_the_views_stay_quick_with_a_full_day_of_changes():
    e = entry()
    runs = {}
    for i in range(300):
        eid = "automation.bulk_%d" % i
        e["snapshot"]["automations"].append({"entity_id": eid, "id": str(i), "mode": "single", "last": NOW - i, "state": "on"})
        runs[eid] = [run(NOW - i * 20 - 60, trig="binary_sensor.hall_motion") for _ in range(hb.MAX_RUN_KEPT)]
    hb.book(e, {"type": "hub_runs", "t": NOW, "items": {}, "runs": runs, "checked": 300}, NOW)
    for i in range(15000):
        hb.book(e, {"type": "hub_state", "entity_id": "switch.desk" if i % 2 else "light.hall", "state": "on" if i % 3 else "off",
                    "t": NOW - i * 5, "by": "automation.bulk_%d" % (i % 300)}, NOW)
    c = hubauto.Ctx(e, NOW)
    t0 = time.perf_counter()
    hubchain.chains(c)
    hubchain.chains(c, entities=["switch.desk"])
    hubchain.mode_events(c, NOW - DAY, NOW)
    assert time.perf_counter() - t0 < 2.0
