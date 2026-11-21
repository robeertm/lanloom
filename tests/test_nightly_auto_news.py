"""N2: the "Automations" section of the nightly report says only what is new (a small register per site), and counts the
runs that ended with an error during the night, read once per report from the hub.

Fixed clock, sample data, fake hub sources; nothing talks to a real hub."""
import asyncio
import json
from pathlib import Path

import pytest

from lanloom import hub_privacy as hp, hubbridge as hb
from lanloom.nightly import autosection, render
from test_hub_auto import entry
from test_hub_bridge_core import HOUR, NOW, Z, add_hub, make_core

ROOT = Path(__file__).resolve().parent.parent
DAY = 86400
RULE_KEYS = ("report.auto.dead", "report.auto.integration", "report.auto.unavailable", "report.auto.self_trigger",
             "report.auto.single_wait")


def section(report):
    (sec,) = [s for s in report["sections"] if s["id"] == "auto"]
    return sec


def keys(sec, point=None):
    return [i["key"] for i in sec["items"] if point is None or i["point"] is point]


def night(rep, n, prev, entry_=None):
    """Report number n (0 = the morning at NOW): the night before it."""
    until = NOW + n * DAY
    return rep.assemble(until - DAY, until, prev, "2026-11-%02d" % (16 + n), [])


def setup(tmp_path, with_runs=None, admin=False):
    c, rep = make_core(tmp_path)
    iid = add_hub(c)
    if admin:
        c.store.execute("UPDATE integrations SET config=? WHERE id=?", (json.dumps({"url": "http://192.0.2.5:8123", "admin_mode": True}), iid))
    e = c.hub[iid] = entry()
    return c, rep, iid, e


def book_runs(e, items, at, status="ok", error=None):
    ev = {"type": "hub_runs", "t": at, "items": items, "checked": 9}
    if error:
        ev["error"] = error
    hb.book(e, ev, at)
    e["run_status"] = {"t": at, "key": status}


# ---------------------------------------------------------------------- 1. only news
def test_a_finding_is_a_point_once_then_one_calm_note(tmp_path):
    c, rep, iid, e = setup(tmp_path)
    r0 = night(rep, 0, None)
    s0 = section(r0)
    assert {"report.auto.dead", "report.auto.integration"} <= set(keys(s0, True))       # first sight: the points as before
    assert "report.auto.ongoing" not in keys(s0) and "report.auto.baseline" not in keys(s0)
    r1 = night(rep, 1, r0)
    s1 = section(r1)
    assert not set(keys(s1)) & set(RULE_KEYS)                                          # the same findings: no point, no line each
    (note,) = [i for i in s1["items"] if i["key"] == "report.auto.ongoing"]
    assert not note["point"] and note["level"] == "info" and note["vars"]["since_at"] == NOW and note["vars"]["n"] >= 5
    r2 = night(rep, 2, r1)
    (note2,) = [i for i in section(r2)["items"] if i["key"] == "report.auto.ongoing"]
    assert note2["vars"]["since_at"] == NOW                                            # "since" keeps the first sighting


def test_a_finding_that_was_gone_for_36_hours_is_new_when_it_comes_back(tmp_path):
    c, rep, iid, e = setup(tmp_path)
    r0 = night(rep, 0, None)
    late = rep.assemble(NOW, NOW + 30 * HOUR, r0, "2026-11-17", [])                    # 30 h later: still the same finding
    assert "report.auto.dead" not in keys(section(late)) and "report.auto.ongoing" in keys(section(late))
    later = rep.assemble(NOW, NOW + 37 * HOUR, r0, "2026-11-17", [])                   # 37 h after the last sighting: new
    assert "report.auto.dead" in keys(section(later), True) and "report.auto.ongoing" not in keys(section(later))
    # it vanished from the hub for two reports, then comes back
    saved = c.hub[iid]
    c.hub[iid] = hb.new_entry(None)
    r1 = night(rep, 1, r0)
    r2 = night(rep, 2, r1)
    assert not [x for x in r2["sections"] if x["id"] == "auto"] and r2["keep"]["auto"][str(iid)]       # nothing to say; the register is carried
    c.hub[iid] = saved
    r3 = night(rep, 3, r2)
    assert "report.auto.dead" in keys(section(r3), True)
    assert all(v == [NOW + 3 * DAY, NOW + 3 * DAY] for k, v in r3["keep"]["auto"][str(iid)].items() if k.startswith("dead:"))


def test_the_first_report_after_the_update_takes_everything_as_known_and_says_so_once(tmp_path):
    c, rep, iid, e = setup(tmp_path)
    old = {"day": "2026-11-15", "since": NOW - 2 * DAY, "until": NOW - DAY, "sections": [], "points": 0}      # a report of the old version
    s = section(night(rep, 0, old))
    assert not set(keys(s)) & set(RULE_KEYS)
    (base,) = [i for i in s["items"] if i["key"] == "report.auto.baseline"]
    assert not base["point"] and base["vars"]["n"] >= 5
    assert "report.auto.conflict" in keys(s, True)                                      # events of the night stay points
    r = night(rep, 0, old)
    r1 = night(rep, 1, r)
    s1 = section(r1)
    assert "report.auto.baseline" not in keys(s1) and "report.auto.ongoing" in keys(s1)  # next night: just the calm note


def test_new_findings_show_up_beside_the_known_ones(tmp_path):
    c, rep, iid, e = setup(tmp_path)
    r0 = night(rep, 0, None)
    hb.book(e, {"type": "hub_snapshot", "t": NOW, "admin": True, "devices": [], "areas": [],
                "entities": e["snapshot"]["entities"], "automations": e["snapshot"]["automations"], "scripts": e["snapshot"]["scripts"],
                "entries": e["snapshot"]["entries"] + [{"domain": "extra_x", "state": "setup_error", "n": 2}],
                "sun": e["snapshot"]["sun"]}, NOW)
    s1 = section(night(rep, 1, r0))
    (new,) = [i for i in s1["items"] if i["key"] == "report.auto.integration"]
    assert new["point"] and new["vars"]["domain"] == "extra_x"
    assert "report.auto.ongoing" in keys(s1)


def test_events_of_the_night_stay_points_every_night(tmp_path):
    c, rep, iid, e = setup(tmp_path)
    r0 = night(rep, 0, None)
    assert "report.auto.conflict" in keys(section(r0), True)
    # the conflict happened within the first window only; the second night has none, the register never hides one
    r1 = night(rep, 1, r0)
    assert "report.auto.conflict" not in keys(section(r1))
    for t in (NOW + DAY - 3000, NOW + DAY - 2980):
        hb.book(e, {"type": "hub_state", "entity_id": "switch.desk", "state": "on" if t < NOW + DAY - 2990 else "off", "t": t,
                    "by": "automation.desk_on" if t < NOW + DAY - 2990 else "automation.desk_off"}, NOW + DAY)
    r1b = night(rep, 1, r0)
    assert "report.auto.conflict" in keys(section(r1b), True)


def test_the_register_lives_in_the_report_per_site_and_goes_with_the_archive(tmp_path):
    c, rep, iid, e = setup(tmp_path)
    r0 = night(rep, 0, None)
    reg = r0["keep"]["auto"][str(iid)]
    assert reg and all(len(v) == 2 and v[0] == v[1] == NOW for v in reg.values())
    assert not any("secret" in k for k in reg)
    r1 = night(rep, 1, r0)
    assert all(v == [NOW, NOW + DAY] for v in r1["keep"]["auto"][str(iid)].values())     # first and last sight
    assert r0["keep"]["auto"][str(iid)] == reg                                            # the old report is not rewritten
    # not part of what the archive shows or a preview returns
    rep.save(dict(r0, created=NOW, delivery={}))
    assert "keep" not in rep.get(r0["day"])
    # a second site has its own register (its own reports, its own store)
    import tempfile
    other, rep2 = make_core(tempfile.mkdtemp())
    j = add_hub(other)
    other.hub[j] = entry()
    s_other = section(night(rep2, 1, night(rep2, 0, None)))
    assert "report.auto.ongoing" in keys(s_other)
    third, rep3 = make_core(tempfile.mkdtemp())
    k = add_hub(third)
    third.hub[k] = entry()
    assert "report.auto.dead" in keys(section(night(rep3, 1, None)), True)                # unaffected by the other sites


def test_the_register_is_cleaned_of_what_was_gone_for_36_hours(tmp_path):
    c, rep, iid, e = setup(tmp_path)
    r0 = night(rep, 0, None)
    r0["keep"]["auto"][str(iid)]["dead:automation.old_one:abc"] = [NOW - 5 * DAY, NOW - 3 * DAY]
    r0["keep"]["auto"][str(iid)]["dead:automation.recent:abc"] = [NOW - DAY, NOW - 6 * HOUR]
    r1 = night(rep, 1, r0)
    reg = r1["keep"]["auto"][str(iid)]
    assert "dead:automation.old_one:abc" not in reg and "dead:automation.recent:abc" in reg


# ---------------------------------------------------------------------- 2. failed runs of the night
def test_a_failed_run_inside_the_window_is_a_point_with_the_display_name_and_a_link(tmp_path):
    c, rep, iid, e = setup(tmp_path, admin=True)
    text = hp.error_text("Unable to find referenced entities switch.desk", lambda eid: "Desk plug")
    book_runs(e, {"automation.hall_motion": [{"at": NOW - 3 * HOUR, "text": text}, {"at": NOW - 5 * HOUR, "text": None}]}, NOW)
    s = section(rep.assemble(NOW - DAY, NOW, None, "2026-11-16", []))
    (p,) = [i for i in s["items"] if i["key"] == "report.auto.run_failed"]
    assert p["point"] and p["level"] == "warn" and p["vars"]["name"] == "Hall motion" and p["vars"]["n"] == 2
    assert p["vars"]["text"] == "Unable to find referenced entities Desk plug" and p["vars"]["last_at"] == NOW - 3 * HOUR
    assert p["href"] == "#/automations/%d/item/automation.hall_motion" % iid
    assert {"key": "report.auto.part.failed_runs", "vars": {"n": 1}} in s["summary"]["parts"]
    assert "switch.desk" not in json.dumps(p["vars"])                                 # the id of the entity in the text is gone


def test_a_failed_run_without_text_has_its_own_sentence_and_old_runs_do_not_count(tmp_path):
    c, rep, iid, e = setup(tmp_path, admin=True)
    book_runs(e, {"automation.hall_motion": [{"at": NOW - 2 * HOUR, "text": None}],
                  "automation.desk_on": [{"at": NOW - 30 * HOUR, "text": "old"}]}, NOW)       # before the window: nothing
    s = section(rep.assemble(NOW - 8 * HOUR, NOW, None, "2026-11-16", []))
    fails = [i for i in s["items"] if i["key"].startswith("report.auto.run_failed")]
    assert [i["key"] for i in fails] == ["report.auto.run_failed_notext"] and "text" not in fails[0]["vars"]
    book_runs(e, {"automation.desk_on": [{"at": NOW - 30 * HOUR, "text": "old"}]}, NOW)
    s = section(rep.assemble(NOW - 8 * HOUR, NOW, None, "2026-11-16", []))
    assert not [i for i in s["items"] if i["key"].startswith("report.auto.run_failed")]
    assert not [p for p in s["summary"].get("parts", []) if p["key"].endswith("failed_runs")]


def test_at_most_five_automations_by_name_the_rest_counted(tmp_path):
    c, rep, iid, e = setup(tmp_path, admin=True)
    ids = ["automation.desk_on", "automation.desk_off", "automation.hall_motion", "automation.ghost", "automation.echo",
           "automation.dawn", "script.night"]
    book_runs(e, {x: [{"at": NOW - HOUR, "text": None}] for x in ids}, NOW)
    s = section(rep.assemble(NOW - 8 * HOUR, NOW, None, "2026-11-16", []))
    named = [i for i in s["items"] if i["key"] == "report.auto.run_failed_notext"]
    (more,) = [i for i in s["items"] if i["key"] == "report.auto.run_failed_more"]
    assert len(named) == 5 and more["vars"]["n"] == 2 and more["point"]
    assert {i["vars"]["name"] for i in named} | set(more["vars"]["names"].split(", ")) <= {"Desk on", "Desk off", "Hall motion", "Ghost bell", "Echo", "Dawn", "Night", "…"} | \
        set(more["vars"]["names"].replace(" …", "").split(", "))


def test_failed_runs_stay_points_every_night(tmp_path):
    c, rep, iid, e = setup(tmp_path, admin=True)
    book_runs(e, {"automation.hall_motion": [{"at": NOW - HOUR, "text": None}]}, NOW)
    r0 = rep.assemble(NOW - DAY, NOW, None, "2026-11-16", [])
    book_runs(e, {"automation.hall_motion": [{"at": NOW + DAY - HOUR, "text": None}]}, NOW + DAY)
    r1 = rep.assemble(NOW, NOW + DAY, r0, "2026-11-17", [])
    assert "report.auto.run_failed_notext" in keys(section(r0), True) and "report.auto.run_failed_notext" in keys(section(r1), True)


def test_an_unreadable_summary_is_one_calm_note_never_an_error(tmp_path):
    c, rep, iid, e = setup(tmp_path, admin=True)
    e["run_status"] = {"t": NOW, "key": "timeout"}
    s = section(rep.assemble(NOW - 8 * HOUR, NOW, None, "2026-11-16", []))
    (note,) = [i for i in s["items"] if i["key"] == "report.auto.runs_unavailable"]
    assert not note["point"] and note["level"] == "info"
    assert s["level"] != "danger" and "report.section_failed" not in json.dumps(s)
    # a hub that answered with an error key: the same note
    book_runs(e, {}, NOW, status="source.hub.request_failed", error="source.hub.request_failed")
    s = section(rep.assemble(NOW - 8 * HOUR, NOW, None, "2026-11-16", []))
    assert "report.auto.runs_unavailable" in keys(s, False)
    # without a status (nothing was asked: no admin mode) there is no note and no failed run
    e.pop("run_status")
    e.pop("run_summary")
    s = section(rep.assemble(NOW - 8 * HOUR, NOW, None, "2026-11-16", []))
    assert not [k for k in keys(s) if k.startswith("report.auto.run")]


def test_a_summary_from_another_time_is_not_used(tmp_path):
    c, rep, iid, e = setup(tmp_path, admin=True)
    book_runs(e, {"automation.hall_motion": [{"at": NOW - HOUR, "text": None}]}, NOW - 2 * DAY)       # read two days ago
    s = section(rep.assemble(NOW - 8 * HOUR, NOW, None, "2026-11-16", []))
    assert not [k for k in keys(s) if k.startswith("report.auto.run")]


# ---------------------------------------------------------------------- 3. asking the hub once per report
class Src:
    def __init__(self, ok=True):
        self.calls, self.ok = 0, ok

    def want_run_summary(self):
        self.calls += 1
        return self.ok


def test_the_hub_is_asked_once_per_report_and_only_in_admin_mode(tmp_path):
    c, rep, iid, e = setup(tmp_path, admin=False)
    src = c._hub_sources[iid] = Src()
    asyncio.run(c.hub_collect_runs(wait_s=0.1))
    assert src.calls == 0 and "run_status" not in e                                    # no admin mode: no request at all
    c.store.execute("UPDATE integrations SET config=? WHERE id=?", (json.dumps({"url": "http://192.0.2.5:8123", "admin_mode": True}), iid))
    asyncio.run(c.hub_collect_runs(wait_s=0.1))
    assert src.calls == 1 and e["run_status"]["key"] == "timeout"                      # asked once; no answer within the wait


def test_an_answer_settles_the_status_and_an_unsupported_or_missing_source_is_not_waited_for(tmp_path):
    c, rep, iid, e = setup(tmp_path, admin=True)
    src = c._hub_sources[iid] = Src()

    async def go():
        task = asyncio.create_task(c.hub_collect_runs(wait_s=5))
        await asyncio.sleep(0.05)
        c.on_event("int:%d" % iid, iid, {"type": "hub_runs", "t": __import__("time").time() + 1,
                                         "items": {"automation.hall_motion": [{"at": NOW, "text": None}]}, "checked": 3})
        await task
    asyncio.run(go())
    assert e["run_status"]["key"] == "ok" and list(e["run_summary"]["items"]) == ["automation.hall_motion"]
    c._hub_sources[iid] = Src(ok=False)
    asyncio.run(c.hub_collect_runs(wait_s=30))                                          # returns at once
    assert e["run_status"]["key"] == "unsupported"
    c._hub_sources.pop(iid)
    asyncio.run(c.hub_collect_runs(wait_s=30))
    assert e["run_status"]["key"] == "offline"


def test_a_report_asks_before_it_is_built_but_a_preview_does_not(tmp_path):
    c, rep, iid, e = setup(tmp_path, admin=True)
    asked = []

    async def collect():
        asked.append(1)
    c.hub_collect_runs = collect
    asyncio.run(rep.build(NOW - DAY, NOW, None, "2026-11-16"))
    assert asked == [1]
    asyncio.run(rep.preview())
    assert asked == [1]

    async def boom():
        raise RuntimeError("hub went away")
    c.hub_collect_runs = boom
    assert asyncio.run(rep.build(NOW - DAY, NOW, None, "2026-11-16"))["sections"]       # a failing read never fails the report


def test_the_summary_booking_keeps_only_valid_items():
    ev = {"type": "hub_runs", "t": NOW, "checked": 4, "items": {
        "automation.ok": [{"at": NOW, "text": "x" * 500}, {"at": "bad"}, {"at": True}], "person.sam": [{"at": NOW, "text": None}],
        "not an id": [{"at": NOW}], "script.fine": "oops"}, "error": "free text of the hub"}
    e = hb.new_entry(None)
    hb.book(e, ev, NOW)
    s = e["run_summary"]
    assert list(s["items"]) == ["automation.ok"] and len(s["items"]["automation.ok"]) == 1 and len(s["items"]["automation.ok"][0]["text"]) <= hp.ERROR_MAX
    assert s["error"] is None and s["checked"] == 4
    assert hb.failed_runs(e, NOW - 10, NOW) == {"automation.ok": s["items"]["automation.ok"]} and hb.failed_runs(e, NOW, NOW + 10) == {}


# ---------------------------------------------------------------------- 4. the error text
@pytest.mark.parametrize("raw,names,want", [
    ("Unable to find referenced entities light.kitchen", {"light.kitchen": "Kitchen light"}, "Unable to find referenced entities Kitchen light"),
    ("Service light.turn_on failed for person.sam", {}, "Service … failed for …"),
    ("No reply from 192.168.1.4 (aa:bb:cc:dd:ee:ff) at https://hub.example/x?token=abc", {}, "No reply from … (…) at …"),
    ("mail sam@example.org token abcdefghijklmnopqrstuvwxyz123456", {}, "mail … token …"),
    ("Error rendering template: {{ states('sensor.x') }}", {}, None),
    ("Jinja2 problem", {}, None),
    ("   ", {}, None), ("…", {}, None), (None, {}, None), (42, {}, None),
])
def test_error_text_keeps_the_gist_and_drops_names_values_and_secrets(raw, names, want):
    assert hp.error_text(raw, names.get) == want


def test_error_text_is_one_short_line_without_control_characters():
    out = hp.error_text("line one\nline two\x00\x1b[31m " + "word " * 80, None)
    assert "\n" not in out and "\x00" not in out and "\x1b" not in out and len(out) <= hp.ERROR_MAX and out.endswith("…")


def test_error_text_hides_private_domains_even_when_a_name_is_known():
    assert hp.error_text("Cannot read camera.door", {"camera.door": "Front door"}.get) == "Cannot read …"
    assert hp.error_text("Cannot read camera.door", {"camera.door": "Front door"}.get, frozenset({"camera"})) == "Cannot read Front door"


# ---------------------------------------------------------------------- 5. words in all six languages
NEW_KEYS = ["report.auto.baseline", "report.auto.ongoing", "report.auto.part.failed_runs", "report.auto.run_failed",
            "report.auto.run_failed_notext", "report.auto.run_failed_more", "report.auto.runs_unavailable", "reports.item.open"]


def test_every_new_text_exists_in_all_six_languages_and_renders_without_leftovers(tmp_path):
    for lang in ("en", "de", "fr", "es", "nl", "it"):
        strings = json.loads((ROOT / "lanloom" / "i18n" / ("%s.json" % lang)).read_text(encoding="utf-8"))
        assert all(strings.get(k) for k in NEW_KEYS), lang
    c, rep, iid, e = setup(tmp_path, admin=True)
    book_runs(e, {"automation.hall_motion": [{"at": NOW - HOUR, "text": "Unable to find referenced entities Desk plug"}],
                  "automation.desk_on": [{"at": NOW - HOUR, "text": None}, {"at": NOW - 2 * HOUR, "text": None}]}, NOW)
    r0 = rep.assemble(NOW - DAY, NOW, None, "2026-11-16", [])
    entries = [i for i in section(r0)["items"]] + [section(r0)["summary"]]
    book_runs(e, {}, NOW + DAY)
    r1 = rep.assemble(NOW, NOW + DAY, r0, "2026-11-17", [])
    old = {"day": "2026-11-15", "since": NOW - 2 * DAY, "until": NOW - DAY, "sections": [], "points": 0}
    entries += list(section(r1)["items"]) + list(section(rep.assemble(NOW - DAY, NOW, old, "2026-11-16", []))["items"])
    e["run_status"] = {"t": NOW + DAY, "key": "timeout"}
    e.pop("run_summary")                                                               # (nothing arrived within the wait)
    entries += section(rep.assemble(NOW, NOW + DAY, r0, "2026-11-17", []))["items"]
    wanted = {"report.auto.run_failed", "report.auto.run_failed_notext", "report.auto.ongoing", "report.auto.baseline", "report.auto.runs_unavailable"}
    assert wanted <= {i["key"] for i in entries}
    for lang in ("en", "de", "fr", "es", "nl", "it"):
        rr = render.Renderer(lang, Z, "2026-11-16")
        texts = {i["key"]: rr.line(i) for i in entries}
        for k in wanted:
            assert "{" not in texts[k] and "}" not in texts[k] and "report." not in texts[k], (lang, k, texts[k])
        assert "Desk plug" in rr.line(next(i for i in entries if i["key"] == "report.auto.run_failed"))


def test_the_ai_still_sees_the_section_only_as_its_summary():
    from lanloom.ai import features
    assert "auto" in features.PRIVATE_SECTIONS


# ---------------------------------------------------------------------- 6. the sample network
def test_the_demo_shows_failed_runs_on_two_nights_and_the_standing_findings_as_known(tmp_path, monkeypatch):
    from lanloom import config as config_mod
    from lanloom.demoview import DemoView
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "0")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    core = DemoView(config_mod.load())._build()
    days = [r["day"] for r in core.reports.listing()["reports"]]
    reps = [core.reports.get(d) for d in sorted(days)]
    secs = [section(r) for r in reps]
    assert [keys(s).count("report.auto.run_failed") for s in secs] == [0, 1, 1]
    assert [i["vars"]["n"] for s in secs[1:] for i in s["items"] if i["key"] == "report.auto.run_failed"] == [1, 2]
    assert "report.auto.dead" in keys(secs[0], True) and "report.auto.dead" not in keys(secs[1]) + keys(secs[2])
    assert "report.auto.ongoing" in keys(secs[1], False) and "report.auto.ongoing" in keys(secs[2], False)
    fail = next(i for i in secs[2]["items"] if i["key"] == "report.auto.run_failed")
    assert fail["vars"]["name"] == "Kitchen climate" and "switch." not in fail["vars"]["text"] and fail["href"].startswith("#/automations/")
    assert all("keep" not in r for r in reps)
