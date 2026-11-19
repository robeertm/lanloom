"""Night report section "Control room" (lanloom/controlroom/report.py): fixed clock, section only with a source, points and the calm line,
and every text of the section renders in English and German without leftovers."""
import json
import re
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from lanloom import core as core_mod
from lanloom.controlroom import report as cr_report                     # noqa: F401 - registers the section
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.nightly import Reports, render
from lanloom.store import Store

TZ = "Europe/Amsterdam"
Z = ZoneInfo(TZ)
I18N = Path(core_mod.__file__).resolve().parent / "i18n"


def at(d, hh=0, mm=0, ss=0, mo=11) -> float:
    return datetime(2026, mo, d, hh, mm, ss, tzinfo=Z).timestamp()


NOW = at(16, 6, 30)
SINCE = at(15, 6, 30)


def make(tmp_path):
    store = Store(":memory:")
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=False, probe=False, release_feed="", image_repo="",
                          cookie_secure="never", trusted_origins=(), trusted_proxies=())
    c = core_mod.Core(cfg, store, Vault(store, b"k" * KEY_BYTES))
    rep = Reports(c, Path(tmp_path), clock=lambda: NOW)
    rep.cert_check = lambda: []
    c.reports = rep
    store.set_setting("report_cfg", {"time": "06:30", "tz": TZ, "keep_days": 30, "link": ""})
    return c, rep


def build(rep):
    r = rep.assemble(SINCE, NOW, None, "2026-11-16", [])
    return r, next((s for s in r["sections"] if s["id"] == "controlroom"), None)


def lines(sec, lang="en"):
    rr = render.Renderer(lang, Z, "2026-11-16")
    return {i["id"]: rr.line(i) for i in sec["items"]}


# ---------------------------------------------------------------------------------------------------- only with a source
def test_no_section_without_a_source(tmp_path):
    c, rep = make(tmp_path)
    r, sec = build(rep)
    assert sec is None


def test_a_calm_night_is_one_line(tmp_path):
    c, rep = make(tmp_path)
    c.controlroom.wan.confirmed = "up"
    c.controlroom.backups.save_chain("a", {"name": "Nightly", "expect_hours": 24}, NOW)
    c.controlroom.backups.report_run("a", True, now=NOW - 3600)
    c.controlroom.backups.save_chain("b", {"name": "Weekly", "expect_hours": 168}, NOW)
    c.controlroom.backups.report_run("b", True, now=NOW - 3600)
    r, sec = build(rep)
    assert sec["items"] == [] and sec["level"] == "ok" and sec["summary"] == {"key": "cr.report.calm", "vars": {"chains": 2}}
    rr = render.Renderer("en", Z, "2026-11-16")
    assert rr.line(sec["summary"]) == "Internet, battery backup and backups are calm (2 backup chains)."


def test_a_source_alone_is_enough_for_the_section(tmp_path):
    c, rep = make(tmp_path)
    c.controlroom.ups.feed("nut", 1, "h", [{"name": "u", "status": "OL"}], NOW - 10)
    r, sec = build(rep)
    assert sec is not None and sec["summary"]["key"] == "cr.report.calm"
    c, rep = make(tmp_path)
    c.controlroom.wan._log(NOW - 100000, "down", "", "", "")
    c.controlroom.wan._log(NOW - 99000, "up", "", "", "")
    r, sec = build(rep)
    assert sec is not None                                                             # an old history is still a source


# ---------------------------------------------------------------------------------------------------- points
def test_internet_ups_and_backup_points(tmp_path):
    c, rep = make(tmp_path)
    cr = c.controlroom
    cr.wan._log(at(16, 3, 12), "down", "", "measured", "")
    cr.wan._log(at(16, 3, 26), "up", "", "measured", "")
    cr.wan._log(at(16, 4, 0), "flapping", "", "gateway", "")
    cr.wan._log(at(16, 4, 30), "steady", "", "gateway", "")
    cr.wan._log(at(16, 5, 0), "failover", "Mobile", "gateway", "")
    cr.wan._log(at(16, 5, 40), "failback", "Fibre", "gateway", "")
    cr.wan._log(at(16, 6, 10), "down", "", "measured", "")                                # still down at 06:30
    cr.ups.store.execute("INSERT INTO ups_log(t, unit, state, charge, runtime_s) VALUES (?,?,?,?,?)", (at(16, 3, 12), "nut:1:u", "battery", 90.0, 1800.0))
    cr.ups.store.execute("INSERT INTO ups_log(t, unit, state, charge, runtime_s) VALUES (?,?,?,?,?)", (at(16, 3, 19), "nut:1:u", "online", 88.0, 1700.0))
    cr.ups.store.execute("INSERT INTO ups_log(t, unit, state, charge, runtime_s) VALUES (?,?,?,?,?)", (at(16, 6, 0), "nut:1:u", "low_battery", 20.0, 100.0))
    r, sec = build(rep)
    pts = {i["id"]: i for i in sec["items"] if i["point"]}
    notes = {i["id"]: i for i in sec["items"] if not i["point"]}
    ids = sorted(pts)
    assert ids == sorted(["cr-wan-down:%d" % at(16, 3, 12), "cr-wan-flap:%d" % at(16, 4, 0), "cr-wan-failover:%d" % at(16, 5, 0),
                          "cr-wan-down:%d" % at(16, 6, 10), "cr-ups:%d" % at(16, 3, 12), "cr-ups:%d" % at(16, 6, 0)])
    assert list(notes) == ["cr-wan-failback:%d" % at(16, 5, 40)]
    assert sec["summary"] == {"key": "cr.report.summary", "vars": {"n": 6}} and sec["level"] == "danger"
    en = lines(sec)
    assert en["cr-wan-down:%d" % at(16, 3, 12)] == "The internet was down from 03:12 to 03:26 (14 min)."
    assert en["cr-wan-down:%d" % at(16, 6, 10)] == "The internet has been down since 06:10 and is not back yet."
    assert en["cr-wan-flap:%d" % at(16, 4, 0)] == "The internet kept dropping from 04:00 to 04:30."
    assert en["cr-wan-failover:%d" % at(16, 5, 0)] == "Switched to the reserve line Mobile at 05:00."
    assert en["cr-wan-failback:%d" % at(16, 5, 40)] == "Back on the main line at 05:40."
    assert en["cr-ups:%d" % at(16, 3, 12)] == "The battery backup carried the load from 03:12 to 03:19 (7 min)."
    assert en["cr-ups:%d" % at(16, 6, 0)] == "The battery backup has been carrying the load since 06:00."
    assert pts["cr-ups:%d" % at(16, 6, 0)]["level"] == "danger" and pts["cr-ups:%d" % at(16, 3, 12)]["level"] == "warn"
    assert pts["cr-wan-flap:%d" % at(16, 4, 0)]["level"] == "warn" and pts["cr-wan-failover:%d" % at(16, 5, 0)]["level"] == "warn"


def test_events_outside_the_window_are_left_out(tmp_path):
    c, rep = make(tmp_path)
    cr = c.controlroom
    cr.wan._log(SINCE - 7200, "down", "", "", "")
    cr.wan._log(SINCE - 3600, "up", "", "", "")
    cr.wan._log(SINCE - 3000, "failover", "Mobile", "", "")
    cr.wan.confirmed = "up"
    r, sec = build(rep)
    assert sec["items"] == [] and sec["summary"]["key"] == "cr.report.calm"
    cr.wan._log(SINCE - 600, "down", "", "", "")                                          # began before the window and is still open
    cr.wan._log(SINCE + 600, "up", "", "", "")
    r, sec = build(rep)
    item = sec["items"][0]
    assert item["key"] == "cr.report.wan_down" and item["vars"]["from_at"] == SINCE and item["vars"]["len_dur"] == 600


def test_backup_points_by_status(tmp_path):
    c, rep = make(tmp_path)
    bk = c.controlroom.backups
    for cid, name in (("f", "Failing"), ("l", "Overdue"), ("u", "Silent"), ("o", "Fine"), ("fn", "Failing never ok")):
        bk.save_chain(cid, {"name": name, "expect_hours": 24}, NOW)
    bk.report_run("f", True, now=NOW - 3 * 86400)
    bk.report_run("f", False, now=NOW - 3600)
    bk.report_run("l", True, now=NOW - 5 * 86400)
    bk.report_run("o", True, now=NOW - 3600)
    bk.report_run("fn", False, now=NOW - 3600)
    r, sec = build(rep)
    by = {i["id"]: i for i in sec["items"]}
    assert set(by) == {"cr-bk:manual:f:failed", "cr-bk:manual:l:late", "cr-bk:manual:u:unknown", "cr-bk:manual:fn:failed"}
    assert by["cr-bk:manual:f:failed"]["key"] == "cr.report.backup_failed_since" and by["cr-bk:manual:f:failed"]["vars"]["last_at"] == NOW - 3 * 86400
    assert by["cr-bk:manual:fn:failed"]["key"] == "cr.report.backup_failed" and "last_at" not in by["cr-bk:manual:fn:failed"]["vars"]
    assert by["cr-bk:manual:l:late"]["key"] == "cr.report.backup_late_since" and by["cr-bk:manual:l:late"]["level"] == "warn"
    u = by["cr-bk:manual:u:unknown"]
    assert u["key"] == "cr.report.backup_unknown" and u["point"] is False and u["level"] == "info"
    assert all(by[k]["point"] for k in by if k != "cr-bk:manual:u:unknown")
    assert sec["summary"]["vars"] == {"n": 3} and sec["level"] == "danger"
    assert [i["id"] for i in sec["items"]][0].endswith(":failed")                           # points first, worst first


def test_a_hypervisor_guest_without_backup_is_a_point_with_its_own_text(tmp_path):
    c, rep = make(tmp_path)
    guest = {"id": "g1", "name": "stray", "kind": "vm", "vmid": 7, "host": "h", "status": "running", "backup": {"last_t": None}}
    c.live_cfg[1] = {"t": time.time() - 5, "state": {"guests": [guest]}}              # Core.live_configs measures the age against the wall clock
    c.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES('controller','hv','{}',1,0)")
    r, sec = build(rep)
    item = next(i for i in sec["items"] if i["key"].startswith("cr.report.backup_none"))
    assert item["point"] is True and "stray" in lines(sec)[item["id"]]


def test_the_report_is_the_same_every_time_it_is_built(tmp_path):
    c, rep = make(tmp_path)
    c.controlroom.wan._log(at(16, 3, 12), "down", "", "", "")
    c.controlroom.wan._log(at(16, 3, 26), "up", "", "", "")
    a = json.dumps(build(rep)[1], sort_keys=True)
    b = json.dumps(build(rep)[1], sort_keys=True)
    assert a == b


# ---------------------------------------------------------------------------------------------------- texts
VARS = {"from_at": at(16, 3, 12), "to_at": at(16, 3, 26), "len_dur": 840, "when_at": at(16, 5, 0), "last_at": at(15, 2, 0), "name": "Sample",
        "n": 2, "chains": 3}


def strings(lang):
    return json.loads((I18N / ("%s.json" % lang)).read_text(encoding="utf-8"))


def report_keys():
    return sorted(k for k in strings("en") if k.startswith("cr.report."))


def test_every_report_key_exists_in_both_languages_and_the_code_uses_only_known_keys():
    en, de = strings("en"), strings("de")
    src = (Path(cr_report.__file__)).read_text(encoding="utf-8")
    used = set(re.findall(r'"(cr\.report\.[a-z_]*[a-z])"', src)) | {"cr.report.wan_" + k for k in ("failover", "failback")} | \
        {"cr.report.backup_%s%s" % (s, suffix) for s in ("failed", "late", "unknown", "none") for suffix in ("", "_since")}
    assert report_keys() and {k for k in en if k.startswith("cr.report.")} == {k for k in de if k.startswith("cr.report.")}
    for k in used:
        assert k in en and k in de, k


@pytest.mark.parametrize("lang", ["en", "de"])
def test_every_report_key_renders_without_leftovers(lang):
    rr = render.Renderer(lang, Z, "2026-11-16")
    for key in report_keys():
        vars_ = {k: v for k, v in VARS.items()}
        text = rr.line({"key": key, "vars": vars_})
        assert text and text != key, key
        assert "{" not in text and "}" not in text and "undefined" not in text and "None" not in text, (key, text)
        assert "Sample" in text or "name" not in strings(lang)[key] or lang == "de", (key, text)


@pytest.mark.parametrize("lang", ["en", "de"])
def test_the_variables_the_code_delivers_fill_every_placeholder(lang):
    """Placeholders in the texts must be covered by what report.py really passes (from/to/len/when/last/name/n/chains)."""
    placeholders = set()
    for key in report_keys():
        placeholders |= set(re.findall(r"\{(\w+)\s*[,}]", strings(lang)[key]))
    assert placeholders <= {"from", "to", "len", "when", "last", "name", "n", "chains"}, placeholders - {"from", "to", "len", "when", "last", "name", "n", "chains"}
