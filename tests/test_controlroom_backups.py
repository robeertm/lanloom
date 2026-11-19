"""Backup chains (lanloom/controlroom/backups.py): the one judge, chains a person declares, learned throughput, chains found in
a hypervisor, a hub and host agents. "Unknown instead of ok" wherever nobody can tell."""
from types import SimpleNamespace

import pytest

from crhelp import T0, FakeCore, ent, gw_entry, hub_entry
from lanloom.controlroom import backups as bk
from lanloom.controlroom.backups import BackupWatch, expect_from_schedule, judge

H = 3600.0
D = 86400.0


def make():
    core = FakeCore()
    return core, BackupWatch(core, clock=lambda: T0)


# ---------------------------------------------------------------------------------------------------- judge
@pytest.mark.parametrize("args,expect", [
    # (last_ok, last_fail, running, expect_s, source_age, now[, ever])
    ((T0 - H, None, False, D, None, T0), ("ok", "ok")),
    ((T0 - 1.5 * D, None, False, D, None, T0), ("ok", "ok")),                      # exactly 1.5 times: still fine
    ((T0 - 1.5 * D - 1, None, False, D, None, T0), ("late", "warn")),
    ((T0 - 2 * D, T0 - 3 * D, False, D, None, T0), ("late", "warn")),             # an old failure does not count after a later success
    ((T0 - 2 * H, T0 - H, False, D, None, T0), ("failed", "danger")),             # failure after the success
    ((None, T0 - H, False, D, None, T0), ("failed", "danger")),
    ((T0 - 2 * H, T0 - 2 * H, False, D, None, T0), ("ok", "ok")),                 # same moment: the success wins
    ((None, None, False, D, None, T0), ("none", "warn")),
    ((None, None, False, D, None, T0, False), ("unknown", "unknown")),
    ((T0 - H, None, True, D, None, T0), ("running", "ok")),
    ((None, T0 - H, True, D, None, T0), ("running", "ok")),                       # a new run is going on although the last one failed
    ((T0 - H, None, False, D, 900.0, T0), ("ok", "ok")),                          # source exactly 15 minutes old: still a reading
    ((T0 - H, None, False, D, 901.0, T0), ("unknown", "unknown")),                # a silent source is never "ok"
    ((None, T0 - H, False, D, 5000.0, T0), ("unknown", "unknown")),
    ((T0 - H, None, False, D, 10.0, T0), ("ok", "ok")),
])
def test_judge_table(args, expect):
    assert judge(*args) == expect


def test_judge_a_silent_source_beats_a_late_backup_too():
    assert judge(T0 - 30 * D, None, False, D, 99999.0, T0) == ("unknown", "unknown")


# ---------------------------------------------------------------------------------------------------- schedules
@pytest.mark.parametrize("text,hours", [
    ("", 168), (None, 168), ("02:00", 24), ("2:30", 24), ("daily", 24), ("*-*-* 02:00", 24), ("*-*-* 03:30", 24), ("*-*-* 03:30:00", 24),
    ("sat 03:00", 168), ("sun", 168), ("Saturday 3:00", 168), ("mon..fri 21:00", 72), ("mon,wed,fri 02:00", 84), ("*:0/15", 3), ("*/6:00", 3),
    ("hourly", 3), ("every hour", 3), ("whatever", 168), ("mon..fri */2:00", 72),
])
def test_expect_from_schedule(text, hours):
    assert expect_from_schedule(text) == hours * H


def test_parse_time():
    assert bk.parse_time(1_800_000_000) == 1_800_000_000.0
    assert bk.parse_time("1800000000") == 1_800_000_000.0
    assert bk.parse_time("2026-11-16T03:00:00+00:00") == 1_794_798_000.0
    assert bk.parse_time("2026-11-16T03:00:00Z") == 1_794_798_000.0
    assert bk.parse_time("2026-11-16T03:00:00") == 1_794_798_000.0                  # no zone: UTC
    for junk in (None, "", "unknown", "unavailable", "none", "soon", 12, "12"):
        assert bk.parse_time(junk) is None


# ---------------------------------------------------------------------------------------------------- chains a person declares
def chain(w, cid="nightly", **kw):
    data = {"name": "Nightly", "source": "Server", "target": "Vault", "expect_hours": 24}
    data.update(kw)
    return w.save_chain(cid, data, T0)


def one(w, now=T0, cid="manual:nightly"):
    return next(c for c in w.chains(now) if c["id"] == cid)


def test_save_validates_everything():
    core, w = make()
    assert chain(w) == {"id": "nightly"}
    for cid, err in (("", "chain.err.id"), ("Bad Id", "chain.err.id"), ("-x", "chain.err.id"), ("a" * 49, "chain.err.id"), ("a/b", "chain.err.id"), ("ü", "chain.err.id")):
        with pytest.raises(ValueError, match=err):
            w.save_chain(cid, {"name": "x", "expect_hours": 24}, T0)
    for data, err in (({"expect_hours": 24}, "chain.err.name"), ({"name": "  ", "expect_hours": 24}, "chain.err.name"),
                      ({"name": "x"}, "chain.err.expect"), ({"name": "x", "expect_hours": 0.5}, "chain.err.expect"),
                      ({"name": "x", "expect_hours": 24 * 90 + 1}, "chain.err.expect"), ({"name": "x", "expect_hours": "soon"}, "chain.err.expect"),
                      ({"name": "x", "expect_hours": float("nan")}, "chain.err.expect")):
        with pytest.raises(ValueError, match=err):
            w.save_chain("ok", data, T0)
    assert [c["id"] for c in w.manual()] == ["nightly"]


def test_save_normalises_and_updates_in_place():
    core, w = make()
    w.save_chain(" Mixed-Case_1 ", {"name": "n" * 100, "source": "s" * 100, "target": "t", "expect_hours": "12"}, T0)
    c = w.manual()[0]
    assert c["id"] == "mixed-case_1" and len(c["name"]) == 64 and len(c["source"]) == 64 and c["expect_s"] == 12 * H and c["created"] == T0
    w.save_chain("mixed-case_1", {"name": "Renamed", "expect_hours": 48}, T0 + 100)
    c = w.manual()[0]
    assert len(w.manual()) == 1 and c["name"] == "Renamed" and c["expect_s"] == 48 * H and c["created"] == T0 and c["source"] == ""


def test_at_most_sixty_declared_chains_but_existing_ones_can_be_changed():
    core, w = make()
    for i in range(60):
        w.save_chain("c%02d" % i, {"name": "C", "expect_hours": 24}, T0)
    with pytest.raises(ValueError, match="chain.err.many"):
        w.save_chain("extra", {"name": "C", "expect_hours": 24}, T0)
    w.save_chain("c07", {"name": "Changed", "expect_hours": 24}, T0)
    assert len(w.manual()) == 60


def test_a_chain_nobody_reported_is_unknown_never_ok():
    core, w = make()
    chain(w)
    c = one(w)
    assert c["status"] == "unknown" and c["level"] == "unknown" and c["editable"] is True and c["kind"] == "manual"
    assert c["last_ok_t"] is None and c["age_s"] is None


def test_report_run_success_failure_and_late():
    core, w = make()
    chain(w)
    assert w.report_run("nightly", True, now=T0 - 2 * H) is True
    assert one(w)["status"] == "ok" and one(w)["age_s"] == 2 * H
    assert one(w, T0 + 2 * D)["status"] == "late"
    w.report_run("nightly", False, now=T0 - H)
    c = one(w)
    assert c["status"] == "failed" and c["level"] == "danger" and c["last_fail_t"] == T0 - H and c["last_ok_t"] == T0 - 2 * H
    w.report_run("nightly", True, now=T0 - 60)
    assert one(w)["status"] == "ok"
    assert w.report_run("missing", True) is False


def test_a_failure_alone_is_failed():
    core, w = make()
    chain(w)
    w.report_run("nightly", False, now=T0 - 10)
    assert one(w)["status"] == "failed"


def test_progress_makes_a_chain_running_and_a_finished_run_ends_it():
    core, w = make()
    chain(w)
    w.report_run("nightly", True, now=T0 - 3 * D)                 # would be late
    assert w.report_progress("nightly", 40, now=T0 - 60) is True
    c = one(w)
    assert c["status"] == "running" and c["level"] == "ok" and c["progress"]["pct"] == 40.0 and c["progress"]["basis"] == "reported"
    assert c["progress"]["eta_s"] is None
    w.report_run("nightly", True, now=T0)
    c = one(w)
    assert c["status"] == "ok" and c["progress"] is None
    assert w.report_progress("nope", 5) is False


@pytest.mark.parametrize("pct,shown", [(0, 0.0), (50, 50.0), (99, 99.0), (100, 99.0), (250, 99.0), (-5, 0.0), ("45", 45.0), ("x", None), (None, None)])
def test_progress_is_never_one_hundred_percent(pct, shown):
    core, w = make()
    chain(w)
    w.report_progress("nightly", pct, now=T0)
    assert one(w)["progress"]["pct"] == shown


def test_a_crashed_script_does_not_stay_running_for_ever():
    core, w = make()
    chain(w)
    w.report_run("nightly", False, now=T0 - 5 * H)
    w.report_progress("nightly", 10, now=T0 - 2 * H)
    assert one(w, T0 - 2 * H + 600)["status"] == "running"
    c = one(w, T0 - 2 * H + bk.RUN_SILENT_S + 1)
    assert c["status"] == "failed"                                       # the earlier failure shows again


def test_a_long_learned_duration_gives_a_longer_silence_allowance():
    core, w = make()
    chain(w)
    w.report_run("nightly", True, size=1e9, duration_s=10 * H, now=T0 - 3 * D)
    w.report_progress("nightly", 10, now=T0)
    assert one(w, T0 + 2 * H)["status"] == "running"
    assert one(w, T0 + 16 * H)["status"] != "running"


def test_delete_removes_everything_of_the_chain():
    core, w = make()
    chain(w)
    w.report_run("nightly", True, size=5e9, duration_s=100, now=T0 - 100)
    w.set_restore_checked("manual:nightly", T0 - 5, T0)
    w.report_progress("nightly", 1, now=T0)
    assert w.delete_chain("nightly") is True
    assert w.delete_chain("nightly") is False
    for table, col in (("backup_chains", "id"), ("backup_runs", "chain"), ("backup_learn", "chain")):
        assert w.store.one("SELECT COUNT(*) AS n FROM %s WHERE %s=?" % (table, col), ("nightly",))["n"] == 0
    assert w.store.one("SELECT COUNT(*) AS n FROM backup_checks")["n"] == 0
    assert w.live == {} and w.chains(T0) == []


def test_runs_older_than_400_days_are_dropped():
    core, w = make()
    chain(w)
    w.report_run("nightly", True, now=T0 - 500 * D)
    w.report_run("nightly", True, now=T0)
    assert w.store.one("SELECT COUNT(*) AS n FROM backup_runs")["n"] == 1


# ---------------------------------------------------------------------------------------------------- restore check
def test_restore_check_is_only_a_date_and_never_changes_the_level():
    core, w = make()
    chain(w)
    w.report_run("nightly", True, now=T0 - 60)
    assert one(w)["restore_hint"] == "never" and one(w)["restore_checked_t"] is None
    w.set_restore_checked("manual:nightly", T0 - 10 * D, T0)
    assert one(w)["restore_hint"] == "ok" and one(w)["level"] == "ok"
    w.set_restore_checked("manual:nightly", T0 - 181 * D, T0)
    c = one(w)
    assert c["restore_hint"] == "old" and c["level"] == "ok" and c["status"] == "ok"
    w.set_restore_checked("manual:nightly", T0 - 180 * D, T0)
    assert one(w)["restore_hint"] == "ok"
    w.set_restore_checked("manual:nightly", None, T0)
    assert one(w)["restore_checked_t"] == T0


@pytest.mark.parametrize("t", [T0 + 3600, T0 - 21 * 365 * D, float("nan"), "x", float("inf")])
def test_restore_check_refuses_nonsense_dates(t):
    core, w = make()
    chain(w)
    with pytest.raises(ValueError, match="chain.err.date"):
        w.set_restore_checked("manual:nightly", t, T0)
    assert w.store.one("SELECT COUNT(*) AS n FROM backup_checks")["n"] == 0


def test_a_deleted_chain_that_comes_back_does_not_inherit_its_restore_date():
    core, w = make()
    chain(w)
    w.report_run("nightly", True, now=T0 - 60)
    w.set_restore_checked("manual:nightly", T0 - 5 * D, T0)
    w.delete_chain("nightly")
    chain(w)
    w.report_run("nightly", True, now=T0 - 60)
    assert one(w)["restore_hint"] == "never"


def test_restore_check_allows_a_minute_of_clock_difference_and_updates():
    core, w = make()
    chain(w)
    w.set_restore_checked("manual:nightly", T0 + 59, T0)
    w.set_restore_checked("manual:nightly", T0 - 5, T0)
    assert w.store.one("SELECT t FROM backup_checks")["t"] == T0 - 5


# ---------------------------------------------------------------------------------------------------- learned throughput
def test_learned_values_are_a_moving_average_and_failures_teach_nothing():
    core, w = make()
    chain(w)
    assert w.learned("nightly") == {"bps": None, "dur_s": None, "size": None, "n": 0}
    w.report_run("nightly", True, size=1000, duration_s=100, now=T0 - 3 * D)
    assert w.learned("nightly") == {"bps": 10.0, "dur_s": 100.0, "size": 1000.0, "n": 1}
    w.report_run("nightly", True, size=1000, duration_s=200, now=T0 - 2 * D)
    L = w.learned("nightly")
    assert L["bps"] == 8.5 and L["dur_s"] == 130.0 and L["size"] == 1000.0 and L["n"] == 2
    w.report_run("nightly", False, size=1, duration_s=1, now=T0 - D)
    assert w.learned("nightly") == L


def test_a_run_without_size_teaches_the_duration_only_and_one_without_duration_nothing():
    core, w = make()
    chain(w)
    w.report_run("nightly", True, size=1000, duration_s=100, now=T0 - 3 * D)
    w.report_run("nightly", True, duration_s=300, now=T0 - 2 * D)
    L = w.learned("nightly")
    assert L["bps"] == 10.0 and L["dur_s"] == 160.0 and L["size"] == 1000.0
    w.report_run("nightly", True, size=5000, now=T0 - D)
    w.report_run("nightly", True, size=5000, duration_s=0, now=T0 - D)
    w.report_run("nightly", True, size=5000, duration_s=-4, now=T0 - D)
    assert w.learned("nightly") == L


def test_progress_from_learned_duration_never_reaches_one_hundred():
    core, w = make()
    chain(w)
    w.report_run("nightly", True, size=1e9, duration_s=1000, now=T0 - 3 * D)
    w.live["nightly"] = {"since": T0 - 500, "pct": None}
    p = one(w)["progress"]
    assert p["basis"] == "learned" and p["pct"] == 50.0 and p["eta_s"] == 500 and p["learned_runs"] == 1 and p["learned_bps"] == 1e6
    w.live["nightly"] = {"since": T0 - 5000, "pct": None}
    p = one(w)["progress"]
    assert p["pct"] == 95.0 and p["eta_s"] == 0


def test_progress_without_anything_learned_has_no_percentage():
    core, w = make()
    chain(w)
    w.live["nightly"] = {"since": T0 - 500, "pct": None}
    p = one(w)["progress"]
    assert p["pct"] is None and p["eta_s"] is None and p["basis"] == "" and p["learned_runs"] == 0


def test_reported_progress_wins_over_the_learned_estimate():
    core, w = make()
    chain(w)
    w.report_run("nightly", True, size=1e9, duration_s=1000, now=T0 - 3 * D)
    w.report_progress("nightly", 10, now=T0 - 500)
    p = one(w)["progress"]
    assert p["basis"] == "reported" and p["pct"] == 10.0 and p["eta_s"] is None


# ---------------------------------------------------------------------------------------------------- hypervisor
def hv_state(guests, jobs=None, storage=None):
    st = {"guests": guests}
    if jobs is not None:
        st["backup_jobs"] = jobs
    if storage is not None:
        st["storage"] = storage
    return st


def guest(gid, name, last_t=None, failed_t=None, kind="vm", vmid=100, backup=True, status="running", **kw):
    g = {"id": gid, "name": name, "kind": kind, "vmid": vmid, "host": "h1", "status": status}
    g.update(kw)
    if backup:
        g["backup"] = {"last_t": last_t, "failed_t": failed_t}
    return g


def hv_core(state, age=60):
    core = FakeCore()
    core.configs = [{"key": "int:7", "name": "Hypervisor", "age_s": age, "state": state}]
    return core, BackupWatch(core, clock=lambda: T0)


def rows(w, now=T0):
    return {c["name"]: c for c in w.chains(now)}


def test_hypervisor_guest_in_a_job_ok_late_failed():
    jobs = [{"id": "j1", "enabled": True, "schedule": "02:00", "storage": "vault", "selection": "all", "guests": ["g1", "g2", "g3"], "excluded": []}]
    storage = [{"id": "s-opaque", "name": "vault", "used": 300, "total": 1000}]
    guests = [guest("g1", "web", last_t=T0 - 5 * H, vmid=101), guest("g2", "db", last_t=T0 - 4 * D, vmid=102, kind="lxc"),
              guest("g3", "mail", last_t=T0 - 10 * H, failed_t=T0 - H, vmid=103)]
    core, w = hv_core(hv_state(guests, jobs, storage))
    r = rows(w)
    assert r["web"]["status"] == "ok" and r["web"]["kind"] == "hypervisor" and r["web"]["source"] == "VM 101" and r["web"]["target"] == "vault"
    assert r["web"]["expect_s"] == D and r["web"]["target_free_pct"] == 70.0 and r["web"]["origin"] == "Hypervisor" and r["web"]["editable"] is False
    assert r["web"]["id"] == "hv:7:g1"
    assert r["db"]["status"] == "late" and r["db"]["source"] == "CT 102"
    assert r["mail"]["status"] == "failed" and r["mail"]["level"] == "danger"


def test_hypervisor_guest_outside_every_job_without_a_backup_is_none():
    jobs = [{"id": "j1", "enabled": True, "schedule": "02:00", "guests": ["g1"], "excluded": []}]
    core, w = hv_core(hv_state([guest("g1", "in-job", last_t=T0 - H), guest("g2", "stray")], jobs))
    r = rows(w)
    assert r["stray"]["status"] == "none" and r["stray"]["level"] == "warn" and r["stray"]["expect_s"] == 7 * D
    assert r["in-job"]["status"] == "ok"


def test_hypervisor_templates_are_no_chains_and_a_stopped_stray_guest_is_only_unknown():
    jobs = [{"id": "j1", "enabled": True, "schedule": "02:00", "guests": [], "excluded": []}]
    core, w = hv_core(hv_state([guest("g1", "tpl", status="stopped", template=1), guest("g2", "parked", status="stopped"), guest("g3", "live")], jobs))
    r = rows(w)
    assert "tpl" not in r and r["parked"]["status"] == "unknown" and r["live"]["status"] == "none"


def test_hypervisor_guest_in_a_job_that_never_ran_is_none_too():
    jobs = [{"id": "j1", "enabled": True, "schedule": "02:00", "guests": ["g1"], "excluded": []}]
    core, w = hv_core(hv_state([guest("g1", "fresh-job")], jobs))
    assert rows(w)["fresh-job"]["status"] == "none"


def test_hypervisor_guest_without_any_backup_facts_is_unknown():
    core, w = hv_core(hv_state([guest("g1", "mystery", backup=False)]))
    assert rows(w)["mystery"]["status"] == "unknown"


def test_hypervisor_selection_all_respects_exclusions_and_disabled_jobs():
    jobs = [{"id": "j1", "enabled": True, "schedule": "sat 03:00", "selection": "all", "excluded": ["g2"], "guests": []},
            {"id": "j2", "enabled": False, "schedule": "02:00", "guests": ["g2"], "excluded": []}]
    guests = [guest("g1", "a", last_t=T0 - 3 * D), guest("g2", "b", last_t=T0 - 3 * D)]
    core, w = hv_core(hv_state(guests, jobs))
    r = rows(w)
    assert r["a"]["expect_s"] == 7 * D and r["b"]["expect_s"] == 7 * D        # b: excluded from j1, j2 is off - default expectation


def test_hypervisor_silent_source_is_unknown_and_stale_state_is_not_used():
    guests = [guest("g1", "a", last_t=T0 - H)]
    core, w = hv_core(hv_state(guests), age=bk.SOURCE_STALE_S + 5)
    assert rows(w)["a"]["status"] == "unknown"
    core, w = hv_core(hv_state(guests), age=86400 + 5)                       # older than a day: not read at all
    assert w.chains(T0) == []


def test_hypervisor_storage_without_size_has_no_free_share():
    jobs = [{"id": "j1", "enabled": True, "schedule": "02:00", "storage": "vault", "selection": "all", "excluded": [], "guests": []}]
    for sto in ([], [{"name": "vault"}], [{"name": "vault", "total": 0, "used": 0}], [{"name": "vault", "total": 100, "used": 250}]):
        core, w = hv_core(hv_state([guest("g1", "a", last_t=T0 - H)], jobs, sto))
        free = rows(w)["a"]["target_free_pct"]
        assert free == (0.0 if sto and sto[0].get("total") == 100 else None)


def test_hypervisor_state_without_guests_gives_no_chains():
    core, w = hv_core({"guests": []})
    assert w.chains(T0) == []


# ---------------------------------------------------------------------------------------------------- hub
def hub_core(states, t=T0 - 30):
    ents = [ent("sensor.backup_manager_state", "dev1", "Backup Manager state"),
            ent("sensor.backup_last_successful", "dev1", "Last successful automatic backup", dc="timestamp"),
            ent("sensor.backup_next", "dev1", "Next scheduled automatic backup", dc="timestamp"),
            ent("event.backup_event", "dev1", "Backup event")]
    core = FakeCore()
    core.hub = {1: hub_entry(ents, {k: (v, t) for k, v in states.items()}, [{"id": "dev1", "name": "Hub"}], t=t)}
    return core, BackupWatch(core, clock=lambda: T0)


def hub_chain(w):
    cs = w.chains(T0)
    assert len(cs) == 1
    return cs[0]


def iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()


def test_hub_backup_ok_late_and_next_run():
    core, w = hub_core({"sensor.backup_manager_state": "idle", "sensor.backup_last_successful": iso(T0 - 5 * H),
                        "sensor.backup_next": iso(T0 + 3 * H)})
    c = hub_chain(w)
    assert c["id"] == "hub:1" and c["name"] == "hub-backup" and c["kind"] == "hub" and c["status"] == "ok" and c["next_t"] == T0 + 3 * H
    core, w = hub_core({"sensor.backup_manager_state": "idle", "sensor.backup_last_successful": iso(T0 - 3 * D)})
    assert hub_chain(w)["status"] == "late"


def test_hub_manager_state_create_backup_is_running():
    for state in ("create_backup", "receive_backup", "in_progress", "creating"):
        core, w = hub_core({"sensor.backup_manager_state": state, "sensor.backup_last_successful": iso(T0 - 3 * D)})
        assert hub_chain(w)["status"] == "running", state
    assert "hub:1" in w.live
    core, w = hub_core({"sensor.backup_manager_state": "idle", "sensor.backup_last_successful": iso(T0 - H)})
    assert hub_chain(w)["status"] == "ok" and w.live == {}


def test_hub_running_state_ends_and_the_duration_is_learned():
    core, w = hub_core({"sensor.backup_manager_state": "create_backup", "sensor.backup_last_successful": iso(T0 - 3 * D)})
    w.chains(T0 - 600)
    assert w.live["hub:1"]["since"] == T0 - 600
    w._track("hub:1", False, None, T0, ended_ok=True)
    assert "hub:1" not in w.live and w.learned("hub:1")["dur_s"] == 600.0 and w.learned("hub:1")["n"] == 1


def test_hub_a_blocked_manager_makes_an_ok_backup_late():
    core, w = hub_core({"sensor.backup_manager_state": "blocked", "sensor.backup_last_successful": iso(T0 - H)})
    c = hub_chain(w)
    assert c["status"] == "late" and c["level"] == "warn"


def test_hub_failed_event_after_the_last_success_is_failed():
    core, w = hub_core({"sensor.backup_manager_state": "idle", "sensor.backup_last_successful": iso(T0 - 5 * H), "event.backup_event": "failed"},
                       t=T0 - 30)
    c = hub_chain(w)
    assert c["status"] == "failed" and c["last_fail_t"] == T0 - 30


def test_hub_silent_source_is_unknown():
    core, w = hub_core({"sensor.backup_manager_state": "idle", "sensor.backup_last_successful": iso(T0 - H)}, t=T0 - 2000)
    assert hub_chain(w)["status"] == "unknown"


def test_hub_without_backup_entities_or_values_gives_no_chain():
    core = FakeCore()
    core.hub = {1: hub_entry([ent("sensor.temperature", "d", "Temperature")], {"sensor.temperature": ("20", T0)})}
    assert BackupWatch(core, clock=lambda: T0).chains(T0) == []
    core, w = hub_core({"sensor.backup_next": iso(T0 + H)})                          # only a next run: nothing to judge
    assert w.chains(T0) == []
    core, w = hub_core({"sensor.backup_last_successful": "unknown"})
    assert hub_chain(w)["status"] == "none"                                           # the hub says: never


# ---------------------------------------------------------------------------------------------------- host agents
def agents_core(timers, t=T0 - 20, host="d-test-3"):
    core = FakeCore()
    core.agents = SimpleNamespace(live={"a1": {"t": t, "units": {"timers": timers}}}, device_of=lambda aid: host)
    return core, BackupWatch(core, clock=lambda: T0)


def timer(name, last=None, nxt=None, **kw):
    d = {"name": name, "active": True}
    if last is not None:
        d["last"] = last
    if nxt is not None:
        d["next"] = nxt
    d.update(kw)
    return d


def test_agent_timers_with_backup_words_become_chains():
    core, w = agents_core([timer("restic-backup.timer", T0 - 5 * H, T0 + 19 * H), timer("apt-daily.timer", T0 - H, T0 + H),
                           timer("pg-dump.timer", T0 - 2 * D, T0 - D + H), timer("zfs-snapshot.timer", T0 - H, T0 + H),
                           timer("borgmatic.timer", T0 - H, T0 + H), timer("rsync-offsite.timer", T0 - H, T0 + H)])
    names = sorted(c["name"] for c in w.chains(T0))
    assert names == ["borgmatic", "pg-dump", "restic-backup", "rsync-offsite", "zfs-snapshot"]
    c = rows(w)["restic-backup"]
    assert c["id"] == "agent:a1:restic-backup" and c["kind"] == "agent" and c["status"] == "ok" and c["expect_s"] == 24 * H and c["device"] == "d-test-3"
    assert rows(w)["pg-dump"]["status"] == "late"                                    # last run 2 days ago, daily rhythm


def test_agent_timer_failed_states():
    core, w = agents_core([timer("a-backup.timer", T0 - H, T0 + H, unit_state="failed"), timer("b-backup.timer", T0 - H, T0 + H, unit_result="exit-code"),
                           timer("c-backup.timer", T0 - H, T0 + H, unit_result="success"), timer("d-backup.timer", T0 - H, T0 + H, result="timeout")])
    r = rows(w)
    assert r["a-backup"]["status"] == "failed" and r["b-backup"]["status"] == "failed" and r["d-backup"]["status"] == "failed"
    assert r["c-backup"]["status"] == "ok"
    assert r["a-backup"]["last_ok_t"] is None and r["a-backup"]["last_fail_t"] == T0 - H


def test_agent_timer_running_and_rhythm_default():
    core, w = agents_core([timer("x-backup.timer", T0 - 3 * D, None, unit_state="activating"), timer("y-backup.timer", T0 - 3 * H)])
    r = rows(w)
    assert r["x-backup"]["status"] == "running" and r["x-backup"]["expect_s"] == D
    assert r["y-backup"]["expect_s"] == D and r["y-backup"]["status"] == "ok"


def test_agent_timer_that_never_ran_is_unknown_and_a_silent_agent_too():
    core, w = agents_core([timer("never-backup.timer", None, T0 + H)])
    assert rows(w)["never-backup"]["status"] == "unknown"
    core, w = agents_core([timer("x-backup.timer", T0 - H, T0 + H)], t=T0 - 2000)
    assert rows(w)["x-backup"]["status"] == "unknown"


def test_agents_missing_or_broken_do_not_matter():
    core = FakeCore()
    assert BackupWatch(core, clock=lambda: T0).chains(T0) == []
    core.agents = SimpleNamespace(live={"a1": {"units": {}}, "a2": {}}, device_of=lambda aid: None)
    assert BackupWatch(core, clock=lambda: T0).chains(T0) == []

    def boom(aid):
        raise RuntimeError("x")
    core, w = agents_core([timer("x-backup.timer", T0 - H, T0 + H)])
    core.agents.device_of = boom
    assert rows(w)["x-backup"]["device"] is None


# ---------------------------------------------------------------------------------------------------- the whole list
def test_view_summary_and_overall_level():
    core, w = make()
    assert w.view(T0) == {"t": T0, "chains": [], "summary": {"ok": 0, "late": 0, "failed": 0, "running": 0, "unknown": 0, "none": 0},
                          "level": "none", "restore_old_days": 180}
    chain(w, "a", name="A")
    chain(w, "b", name="B")
    chain(w, "c", name="C")
    chain(w, "d", name="D")
    w.report_run("a", True, now=T0 - H)
    w.report_run("b", False, now=T0 - H)
    w.report_run("c", True, now=T0 - 9 * D)
    v = w.view(T0)
    assert v["summary"] == {"ok": 1, "late": 1, "failed": 1, "running": 0, "unknown": 1, "none": 0} and v["level"] == "danger"
    assert [c["name"] for c in v["chains"]] == ["B", "C", "D", "A"]                 # danger, warn, unknown, ok
    w.delete_chain("b")
    assert w.view(T0)["level"] == "warn"
    w.delete_chain("c")
    assert w.view(T0)["level"] == "unknown"                                          # an unknown chain next to an ok one is not "ok"
    w.report_run("d", True, now=T0 - H)
    assert w.view(T0)["level"] == "ok"


def test_chains_of_all_kinds_side_by_side_are_sorted_and_capped():
    core, w = hv_core(hv_state([guest("g1", "zz-guest", last_t=T0 - H)]))
    chain(w, "m", name="aa-manual")
    w.report_run("m", True, now=T0 - H)
    assert [c["name"] for c in w.chains(T0)] == ["aa-manual", "zz-guest"]
    many = [guest("g%d" % i, "g%d" % i, last_t=T0 - H, vmid=i) for i in range(350)]
    core, w = hv_core(hv_state(many))
    assert len(w.chains(T0)) == bk.MAX_CHAINS
