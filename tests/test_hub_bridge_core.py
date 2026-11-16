"""Core side of the home-automation hub bridge: bookkeeping, views, privacy filter, nightly section, AI privacy, API.

Everything runs against a fixed clock (``NOW``); nothing sleeps and nothing talks to a hub. Names are sample data.
"""
import json
import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod, core as core_mod, demo_hub, hub_privacy as hp, hubbridge as hb, live
from lanloom.ai import features
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.main import create_app
from lanloom.nightly import Reports, render
from lanloom.store import Store

NOW = 1_800_000_000.0          # fixed clock; NOW // 3600 is an hour boundary + a few minutes
HOUR = hb.HOUR
H0 = (int(NOW) // HOUR) * HOUR  # start of the current hour bucket
HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"


def trig(eid, n, last):
    return {"type": "hub_triggers", "runs": {eid: {"n": n, "last": last}}}


def snapshot(**extra):
    snap = {"type": "hub_snapshot",
            "entities": [{"entity_id": "automation.alpha", "name": "Alpha for Sam"},
                         {"entity_id": "automation.beta", "name": "Beta"},
                         {"entity_id": "script.one", "name": "Script One"},
                         {"entity_id": "script.two", "name": "Script Two"}],
            "automations": [{"entity_id": "automation.alpha", "id": "11", "mode": "single", "last": None, "state": "on"},
                            {"entity_id": "automation.beta", "id": None, "mode": "queued", "last": None, "state": "on"}],
            "scripts": [{"entity_id": "script.one", "id": "one", "mode": "single", "last": None, "state": "off"},
                        {"entity_id": "script.two", "id": "two", "mode": "single", "last": None, "state": "off"}],
            "devices": [], "areas": []}
    snap.update(extra)
    return snap


def filled_entry(now=NOW):
    """One runaway automation, one busy script, one script loop, one blocked lookup."""
    NOW = now
    e = hb.new_entry(None)
    hb.book(e, snapshot(), NOW)
    hb.book(e, trig("automation.alpha", hb.BUSY_PER_HOUR + 5, NOW - 600), NOW)
    hb.book(e, trig("automation.beta", 2, NOW - 300), NOW)
    hb.book(e, trig("script.one", 4, NOW - 200), NOW)
    hb.book(e, {"type": "hub_deps", "t": NOW - 30,
                "automations": {"automation.alpha": {"trigger": ["a"], "condition": [], "action": ["b", "c"], "related": []}},
                "scripts": {}, "cycles": [["script.one", "script.two"]],
                "blocked": [{"entity_id": "automation.beta", "kind": "config", "until": NOW + 3600, "n": 2}]}, NOW)
    return e


# ---------------------------------------------------------------------- 1. book / runs_between / busiest_hour
def test_book_buckets_runs_per_hour_and_sums_within_a_bucket():
    e = hb.new_entry(None)
    hb.book(e, trig("automation.alpha", 3, H0 + 10), NOW)
    hb.book(e, trig("automation.alpha", 4, H0 + 20), NOW)           # same hour: summed
    hb.book(e, trig("automation.alpha", 5, H0 - 100), NOW)          # previous hour
    assert e["runs"]["automation.alpha"]["hours"] == {H0 // HOUR: 7, H0 // HOUR - 1: 5}
    assert e["runs"]["automation.alpha"]["last"] == H0 - 100


def test_book_ignores_invalid_entities_and_prunes_after_26_hours():
    e = hb.new_entry(None)
    ev = {"type": "hub_triggers", "runs": {"not an id": {"n": 1}, "automation.ok": "x", "automation.fine": {"n": 2, "last": NOW}}}
    hb.book(e, ev, NOW)
    assert list(e["runs"]) == ["automation.fine"]
    old = NOW - 30 * HOUR
    hb.book(e, trig("automation.fine", 9, old), old)                # booked 30 h ago ...
    assert (old // HOUR) * HOUR // HOUR in e["runs"]["automation.fine"]["hours"]
    hb.book(e, trig("automation.fine", 1, NOW), NOW)                # ... and pruned by the next booking now
    hours = e["runs"]["automation.fine"]["hours"]
    assert min(hours) >= int(NOW // HOUR) - hb.KEEP_HOURS and sum(hours.values()) == 3


def test_book_state_and_by_and_bridge_and_traces():
    e = hb.new_entry(None)
    hb.book(e, {"type": "hub_state", "entity_id": "light.a", "state": "on", "by": "automation.alpha", "t": NOW}, NOW)
    hb.book(e, {"type": "hub_state", "entity_id": "light.b", "state": "off"}, NOW)
    assert e["states"]["light.a"] == ("on", NOW) and e["states"]["light.b"] == ("off", NOW)
    assert e["by"] == {"light.a": ("automation.alpha", NOW)}
    hb.book(e, {"type": "hub_bridge", "refused": 1}, NOW)
    assert e["bridge"]["refused"] == 1
    hb.book(e, {"type": "hub_traces", "entity_id": "bad id", "runs": []}, NOW)
    assert e["traces"] == {}
    for i in range(hb.MAX_TRACES + 5):
        hb.book(e, {"type": "hub_traces", "entity_id": "script.s%d" % i, "runs": []}, NOW)
    assert len(e["traces"]) == hb.MAX_TRACES and "script.s0" not in e["traces"]


def test_runs_between_window_boundaries():
    e = hb.new_entry(None)
    for k, n in ((-3, 1), (-2, 2), (-1, 4), (0, 8)):
        t = H0 + k * HOUR + 5
        hb.book(e, trig("automation.alpha", n, t), t)
    since, until = H0 - 2 * HOUR + 1, H0 + 1                       # a bucket counts when it starts inside the window
    assert hb.runs_between(e, since, until) == {"automation.alpha": 2 + 4 + 8}
    assert hb.runs_between(e, since, H0 - HOUR + 1) == {"automation.alpha": 2 + 4}
    assert hb.runs_between(e, H0 + HOUR, H0 + 2 * HOUR) == {}       # nothing in the window: absent, not 0


def test_busiest_hour_picks_the_peak_inside_the_window():
    e = hb.new_entry(None)
    for k, n in ((-5, 90), (-2, 7), (-1, 30), (0, 12)):
        t = H0 + k * HOUR + 5
        hb.book(e, trig("automation.alpha", n, t), t)
    assert hb.busiest_hour(e, H0 - 3 * HOUR, NOW) == {"automation.alpha": (30, H0 - HOUR)}
    assert hb.busiest_hour(e, H0 - 6 * HOUR, NOW)["automation.alpha"] == (90, H0 - 5 * HOUR)
    assert hb.busiest_hour(e, NOW + HOUR, NOW + 2 * HOUR) == {}


# ---------------------------------------------------------------------- 2. automations_view
def test_automations_view_order_refs_flags_and_traceable():
    view = hb.automations_view(filled_entry(), NOW)
    assert [a["entity_id"] for a in view] == ["automation.alpha", "script.one", "automation.beta", "script.two"]
    by = {a["entity_id"]: a for a in view}
    alpha = by["automation.alpha"]
    assert alpha["runs_hour"] == hb.BUSY_PER_HOUR + 5 and alpha["runs_day"] == alpha["runs_hour"]
    assert alpha["refs"] == {"trigger": 1, "condition": 0, "action": 2, "related": 0}
    assert by["script.two"]["refs"] is None
    assert alpha["name"] == "Alpha for Sam" and alpha["kind"] == "automation" and by["script.one"]["kind"] == "script"
    assert by["automation.beta"]["blocked"] and not alpha["blocked"]
    assert by["script.one"]["loop"] and by["script.two"]["loop"] and not alpha["loop"]
    assert alpha["traceable"] and by["script.one"]["traceable"] and not by["automation.beta"]["traceable"]   # no config id
    assert alpha["last"] == NOW - 600


def test_automations_view_counts_only_the_last_hour_in_runs_hour():
    e = hb.new_entry(None)
    hb.book(e, snapshot(), NOW)
    t = NOW - 5 * HOUR
    hb.book(e, trig("script.one", 10, t), t)
    row = next(a for a in hb.automations_view(e, NOW) if a["entity_id"] == "script.one")
    assert row["runs_hour"] == 0 and row["runs_day"] == 10


# ---------------------------------------------------------------------- 3. bridge_view
def fake_core(config):
    return SimpleNamespace(integrations=lambda: [{"id": 7, "name": "Sample hub", "kind": "hub", "config": config}])


def test_bridge_view_counts_privacy_runaway_cycles_blocked():
    e = filled_entry()
    e["snapshot"]["devices"] = [{"id": "d1", "macs": ["aa:bb:cc:dd:ee:ff"]}, {"id": "d2"}]
    e["snapshot"]["trackers"] = [{"mac": None, "ip": None}]
    hb.book(e, {"type": "hub_bridge", "private_dropped": 23, "refused": 2, "sent": {"get_states": 3},
                "budgets": {"config": {"waits": 2}, "trace": {"waits": 1}}}, NOW)
    v = hb.bridge_view(fake_core({"admin_mode": True, "privacy_cameras": True}), 7, e, NOW)
    assert v["id"] == 7 and v["name"] == "Sample hub" and v["admin"] is True
    assert v["counts"] == {"devices": 2, "entities": 4, "areas": 0, "automations": 2, "scripts": 2, "trackers": 1, "with_mac": 1}
    assert v["snapshot_age_s"] == 0.0 and v["deps_age_s"] == 30.0
    assert v["privacy"]["shown"] == ["camera", "image"] and v["privacy"]["dropped"] == 23
    assert "person" in v["privacy"]["hidden"] and "camera" not in v["privacy"]["hidden"]
    assert v["privacy"]["groups"]["cameras"] is True and v["privacy"]["groups"]["presence"] is False
    assert v["runaway"] == ["automation.alpha"]
    assert v["cycles"] == [[{"entity_id": "script.one", "name": "Script One"}, {"entity_id": "script.two", "name": "Script Two"}]]
    assert v["blocked"] == [{"entity_id": "automation.beta", "name": "Beta", "kind": "config", "until": NOW + 3600, "n": 2}]
    assert [b["entity_id"] for b in v["busiest"]] == ["automation.alpha", "script.one", "automation.beta"]
    assert v["protection"]["refused"] == 2 and v["protection"]["budget_waits"] == 3 and v["protection"]["templates"] is False
    assert "auth" not in v["allowed"]


def test_bridge_view_empty_entry_and_unknown_connection():
    v = hb.bridge_view(fake_core({}), 99, hb.new_entry(None), NOW)
    assert v["name"] is None and v["admin"] is False and v["snapshot_age_s"] is None and v["deps_age_s"] is None
    assert v["runaway"] == [] and v["cycles"] == [] and v["blocked"] == [] and v["busiest"] == []
    assert v["privacy"]["shown"] == [] and v["privacy"]["dropped"] == 0


def test_runaway_threshold_is_inclusive():
    e = hb.new_entry(None)
    hb.book(e, snapshot(), NOW)
    hb.book(e, trig("automation.alpha", hb.BUSY_PER_HOUR, NOW), NOW)
    hb.book(e, trig("automation.beta", hb.BUSY_PER_HOUR - 1, NOW), NOW)
    assert hb.bridge_view(fake_core({}), 7, e, NOW)["runaway"] == ["automation.alpha"]


# ---------------------------------------------------------------------- 4. hub_privacy
PRIVATE_IDS = ["person.sam", "device_tracker.phone", "zone.home", "camera.door", "image.snap", "calendar.family", "todo.list",
               "persistent_notification.n1", "notify.mobile"]


def test_outbound_drops_private_domains_and_macs_even_if_present():
    snap = {"entities": [{"entity_id": i, "name": "x"} for i in PRIVATE_IDS] + [{"entity_id": "light.kitchen", "name": "Kitchen"}],
            "trackers": [{"mac": "aa:bb:cc:dd:ee:ff"}],
            "devices": [{"id": "d1", "name": "Lamp", "macs": ["aa:bb:cc:dd:ee:ff"]}], "areas": [{"name": "Hall"}], "version": "x"}
    out = hp.outbound(snap)
    assert [e["entity_id"] for e in out["entities"]] == ["light.kitchen"]
    assert out["devices"] == [{"id": "d1", "name": "Lamp"}] and "trackers" not in out and out["version"] == "x"
    assert hp.outbound(None) == {"entities": [], "devices": []}
    assert json.dumps(snap["devices"]).count("macs") == 1                          # the input is not modified


def test_redact_snapshot_blanks_names_but_keeps_entity_ids():
    snap = {"entities": [{"entity_id": "light.kitchen", "name": "Sam's lamp"}, {"entity_id": "sensor.t", "name": None}],
            "devices": [{"id": "d1", "name": "Sam's phone", "macs": ["aa:bb:cc:dd:ee:ff"]}],
            "areas": [{"name": "Sam's room", "id": "a1"}]}
    out = hp.redact_snapshot(snap)
    assert out["entities"] == [{"entity_id": "light.kitchen", "name": hp.REDACTED}, {"entity_id": "sensor.t", "name": None}]
    assert out["devices"] == [{"id": "d1", "name": hp.REDACTED}]
    assert out["areas"] == [{"name": hp.REDACTED, "id": "a1"}]
    assert "Sam" not in json.dumps(out) and "aa:bb" not in json.dumps(out)


def test_shown_domains_only_reacts_to_true():
    assert hp.shown_domains(None) == frozenset() and hp.shown_domains({}) == frozenset()
    assert hp.shown_domains({"privacy_presence": "yes", "privacy_cameras": 1, "privacy_calendars": "true"}) == frozenset()
    assert hp.shown_domains({"privacy_presence": True}) == frozenset({"person", "device_tracker", "zone"})
    both = hp.shown_domains({"privacy_cameras": True, "privacy_notifications": True})
    assert both == frozenset({"camera", "image", "persistent_notification", "notify"})
    assert hp.is_private("person.sam") and not hp.is_private("person.sam", frozenset({"person"})) and not hp.is_private("light.a")


def test_tracker_link_keeps_only_mac_and_ip_normalised():
    attrs = {"mac": "AA-BB-CC-DD-EE-FF", "ip": " 192.168.50.20 ", "latitude": 52.1, "longitude": 4.3, "source_type": "router",
             "state": "home"}
    link = hp.tracker_link("device_tracker.phone", attrs)
    assert link == {"entity_id": "device_tracker.phone", "mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.50.20"}
    assert hp.tracker_link("device_tracker.phone", {"latitude": 1, "mac": "nonsense", "ip": "x"}) is None
    assert hp.tracker_link("light.lamp", {"mac": "aa:bb:cc:dd:ee:ff"}) is None
    assert hp.tracker_link("device_tracker.phone", "not a dict") is None
    assert hp.tracker_link("device_tracker.phone", {"ip": "192.168.50.21"})["mac"] is None


def test_device_macs_normalises_and_ignores_other_connections():
    conns = [["mac", "AA-BB-CC-DD-EE-01"], ["mac", "aa:bb:cc:dd:ee:01"], ["zigbee", "00:11:22:33:44:55"], ["mac", "bogus"],
             ["mac"], "junk", ["mac", 5], ("mac", "aa:bb:cc:dd:ee:02")]
    assert hp.device_macs(conns) == ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]
    assert hp.device_macs(None) == [] and hp.device_macs("aa:bb:cc:dd:ee:01") == []
    assert len(hp.device_macs([["mac", "aa:bb:cc:dd:ee:%02x" % i] for i in range(20)])) == 8


# ---------------------------------------------------------------------- 5. nightly section
TZ = "Europe/Amsterdam"
Z = ZoneInfo(TZ)
SINCE, UNTIL = NOW - 8 * HOUR, NOW


def make_core(tmp_path):
    store = Store(":memory:")
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=False, probe=False,
                          release_feed="", image_repo="", cookie_secure="never", trusted_origins=(), trusted_proxies=())
    c = core_mod.Core(cfg, store, Vault(store, b"k" * KEY_BYTES))
    rep = Reports(c, Path(tmp_path), clock=lambda: NOW)
    rep.cert_check = lambda: []
    c.reports = rep
    store.set_setting("report_cfg", {"time": "06:30", "tz": TZ, "keep_days": 30, "link": ""})
    return c, rep


def add_hub(c, name="Sample hub", enabled=1):
    return c.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES('hub',?,?,?,0)",
                           (name, json.dumps({"url": "http://192.0.2.5:8123"}), enabled)).lastrowid


def hub_section(report):
    return [s for s in report["sections"] if s["id"] == "hub"]


def test_nightly_hub_section_points_level_summary_and_parts(tmp_path):
    c, rep = make_core(tmp_path)
    hid = add_hub(c)
    c.hub[hid] = filled_entry()
    r = rep.assemble(SINCE, UNTIL, None, "2026-11-16", [])
    (sec,) = hub_section(r)
    assert sec["level"] == "warn" and sec["title"] == "report.sec.hub"
    points = {i["key"] for i in sec["items"] if i["point"]}
    assert points == {"report.hub.runaway", "report.hub.loop"}
    assert {i["key"] for i in sec["items"] if not i["point"]} == {"report.hub.blocked", "report.hub.busy"}
    assert sec["summary"]["key"] == "report.hub.summary"
    assert sec["name"] == "Sample hub"
    assert sec["summary"]["vars"] == {"n": 3, "runs": hb.BUSY_PER_HOUR + 5 + 2 + 4}
    assert [p["key"] for p in sec["summary"]["parts"]] == ["report.hub.part.runaway", "report.hub.part.loops",
                                                          "report.hub.part.blocked"]
    runaway = next(i for i in sec["items"] if i["key"] == "report.hub.runaway")
    assert runaway["vars"]["n"] == hb.BUSY_PER_HOUR + 5 and runaway["vars"]["name"] == "Alpha for Sam"
    assert r["points"] >= 2


def test_nightly_hub_section_quiet_without_runs(tmp_path):
    c, rep = make_core(tmp_path)
    hid = add_hub(c)
    c.hub[hid] = hb.new_entry(None)
    hb.book(c.hub[hid], snapshot(), NOW)
    r = rep.assemble(SINCE, UNTIL, None, "2026-11-16", [])
    (sec,) = hub_section(r)
    assert sec["summary"]["key"] == "report.hub.quiet" and sec["summary"]["vars"]["n"] == 2 and sec["name"] == "Sample hub"
    assert not [i for i in sec["items"] if i["point"]] and sec["level"] == "ok"


def test_nightly_hub_section_absent_without_hub_or_snapshot_or_when_disabled(tmp_path):
    c, rep = make_core(tmp_path)
    assert hub_section(rep.assemble(SINCE, UNTIL, None, "2026-11-16", [])) == []
    hid = add_hub(c)
    assert hub_section(rep.assemble(SINCE, UNTIL, None, "2026-11-16", [])) == []     # connection without a snapshot
    c.hub[hid] = filled_entry()
    c.store.execute("UPDATE integrations SET enabled=0 WHERE id=?", (hid,))
    assert hub_section(rep.assemble(SINCE, UNTIL, None, "2026-11-16", [])) == []


def test_nightly_hub_section_one_section_per_connection(tmp_path):
    c, rep = make_core(tmp_path)
    a, b = add_hub(c, "First hub"), add_hub(c, "Second hub")
    c.hub[a], c.hub[b] = filled_entry(), filled_entry()
    secs = hub_section(rep.assemble(SINCE, UNTIL, None, "2026-11-16", []))
    assert sorted(s["name"] for s in secs) == ["First hub", "Second hub"]


def test_nightly_hub_section_renders_in_both_languages_without_placeholders(tmp_path):
    c, rep = make_core(tmp_path)
    c.hub[add_hub(c)] = filled_entry()
    r = rep.assemble(SINCE, UNTIL, None, "2026-11-16", [])
    (sec,) = hub_section(r)
    texts = {}
    for lang in ("en", "de"):
        rr = render.Renderer(lang, Z, "2026-11-16")
        texts[lang] = [rr.line(sec["summary"])] + [rr.line(i) for i in sec["items"]]
        for line in texts[lang]:
            assert "{" not in line and "}" not in line and "report." not in line, line
    assert all(en != de for en, de in zip(texts["en"], texts["de"])) and len(texts["en"]) == len(texts["de"]) >= 5
    assert any("Alpha for Sam" in t and "ran" in t for t in texts["en"])
    assert any("Alpha for Sam" in t and "lief" in t for t in texts["de"])


# ---------------------------------------------------------------------- 6. AI privacy
def test_ai_report_lines_show_only_the_summary_of_the_hub_section():
    secret = "Alpha for Sam"
    report = {"day": "2026-11-16", "sections": [
        {"id": "hub", "title": "report.sec.hub",
         "summary": {"key": "report.hub.summary", "vars": {"n": 3, "runs": 71}},
         "items": [{"id": "x", "key": "report.hub.runaway", "vars": {"name": secret, "n": 65, "at_at": 1}, "level": "warn", "point": True}]},
        {"id": "other", "title": "report.sec.hub",
         "summary": {"key": "report.hub.quiet", "vars": {"n": 1}},
         "items": [{"id": "y", "key": "report.hub.runaway", "vars": {"name": "Visible", "n": 61, "at_at": 1}, "level": "warn", "point": True}]}]}
    reports = SimpleNamespace(tz=lambda: Z)
    lines = features.report_lines(report, reports)
    text = "\n".join(lines)
    assert secret not in text
    assert any("3 automations and scripts ran 71 times" in ln for ln in lines)
    assert any("Visible" in ln and "(needs a look)" in ln for ln in lines)


# ---------------------------------------------------------------------- 7. API
def mkapp(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    return create_app(config_mod.load())


@pytest.fixture()
def api(tmp_path, monkeypatch):
    app = mkapp(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://lanloom.test") as client:
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        assert client.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200
        st = app.state.ll
        client.st = st
        client.hub_id = st.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES('hub',?,?,1,0)",
                                         ("Sample hub", json.dumps({"url": "http://192.0.2.5:8123"}))).lastrowid
        # demo mode answers trace requests from sample data; the hub entry is filled with known content
        st.core.hub[client.hub_id] = filled_entry(time.time())   # the API views use the wall clock
        for role in ("viewer", "operator"):
            st.accounts.create(role + "1", PW, role=role)
        yield client


def login(c, user):
    c.cookies.clear()
    r = c.post("/api/login", json={"username": user, "password": PW}, headers=HDR)
    assert r.status_code == 200, r.text


def test_api_bridge_and_automations_as_viewer(api):
    login(api, "viewer1")
    hid = api.hub_id
    r = api.get("/api/hub/%d/bridge" % hid)
    assert r.status_code == 200
    body = r.json()
    assert {"id", "name", "admin", "counts", "privacy", "protection", "busiest", "runaway", "cycles", "blocked"} <= set(body)
    assert body["id"] == hid and body["runaway"] == ["automation.alpha"]
    r = api.get("/api/hub/%d/automations" % hid)
    assert r.status_code == 200
    autos = r.json()["automations"]
    assert autos[0]["entity_id"] == "automation.alpha" and {"runs_hour", "refs", "blocked", "loop", "traceable"} <= set(autos[0])


def test_api_unknown_connection_is_404_missing(api):
    login(api, "viewer1")
    for path in ("bridge", "automations"):
        r = api.get("/api/hub/9999/" + path)
        assert r.status_code == 404 and r.json()["error"] == "hub.err.missing"
    login(api, "operator1")
    r = api.post("/api/hub/9999/traces/automation.alpha", json={}, headers=HDR)
    assert r.status_code == 404 and r.json()["error"] == "hub.err.missing"


def test_api_traces_post_needs_operator_and_is_audited(api):
    hid, eid = api.hub_id, "automation.hallway_light_on_motion"       # a traceable sample item of the demo
    path = "/api/hub/%d/traces/%s" % (hid, eid)
    login(api, "viewer1")
    r = api.post(path, json={}, headers=HDR)
    assert r.status_code == 403 and r.json()["error"] == "auth.err.role"
    assert api.get(path).json() == {"traces": None}                      # nothing asked for yet
    denied = [e for e in api.st.audit.query({}, 100) if e["action"] == "hub.traces"]
    assert [e["detail"] for e in denied] == [{"need": "operator"}]           # the refusal itself is audited, not a fetch
    login(api, "operator1")
    r = api.post(path, json={}, headers=HDR)
    assert r.status_code == 200 and r.json() == {"queued": 1}
    got = api.get(path).json()["traces"]
    assert got["entity_id"] == eid and len(got["runs"]) == 5 and got["steps"][0]["path"] == "trigger/0"
    acts = [(e["action"], e["actor"]) for e in api.st.audit.query({}, 100) if e["action"] == "hub.traces"]
    assert ("hub.traces", "operator1") in acts and ("hub.traces", "viewer1") in acts and len(acts) == 2
    login(api, "viewer1")
    assert api.get(path).status_code == 200                              # viewers may read what was fetched


def test_api_traces_post_rejects_items_that_are_not_traceable(api):
    login(api, "operator1")
    hid = api.hub_id
    for eid in ("light.kitchen", "person.sam", "automation.garage_door_reminder", "automation.unknown_thing"):
        r = api.post("/api/hub/%d/traces/%s" % (hid, eid), json={}, headers=HDR)
        assert r.status_code == 409 and r.json()["error"] == "hub.err.item", eid


# ---------------------------------------------------------------------- 8. clean_integration
def test_clean_integration_hub_coerces_flags_and_drops_unknown_fields():
    cfg, sec = core_mod.Core.clean_integration("hub", {
        "url": "http://192.0.2.5:8123/", "admin_mode": "yes", "allow_self_signed": True,
        "privacy_presence": True, "privacy_cameras": "yes", "privacy_calendars": 1, "privacy_notifications": False,
        "evil": "x", "token": "should-not-be-plain"}, {"token": "T", "other": "dropped"})
    assert cfg == {"url": "http://192.0.2.5:8123", "admin_mode": False, "allow_self_signed": True, "privacy_presence": True,
                   "privacy_cameras": False, "privacy_calendars": False, "privacy_notifications": False}
    assert sec == {"token": "T"}
    cfg, _ = core_mod.Core.clean_integration("hub", {"url": "https://192.0.2.5", "admin_mode": True}, {})
    assert cfg["admin_mode"] is True and "privacy_presence" not in cfg


def test_clean_integration_hub_rejects_bad_url():
    with pytest.raises(ValueError, match="integration.err.url"):
        core_mod.Core.clean_integration("hub", {"url": "ftp://x"}, {})
