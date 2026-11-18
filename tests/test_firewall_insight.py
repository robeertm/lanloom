"""S6: the rules in plain words, "May A reach B?", checks with a suggestion, the hit log and the board data."""
import json
from pathlib import Path

import pytest

from lanloom.demo_live import live_sample, policy_hits
from lanloom.firewall import insight as fi
from lanloom.store import Store

I18N = json.loads((Path(fi.__file__).resolve().parents[1] / "i18n" / "en.json").read_text(encoding="utf-8"))


@pytest.fixture()
def st():
    return live_sample(1_800_000_000)


def pol(st, pid):
    return next(p for p in st["policies"] if p["id"] == pid)


def keys_of(msg):
    """Every translation key a message (and its nested messages) uses."""
    if isinstance(msg, dict) and "key" in msg:
        found = {msg["key"]}
        for v in (msg.get("vars") or {}).values():
            found |= keys_of(v)
        for n in msg.get("notes") or []:
            found |= keys_of(n)
        return found
    return set()


# ---------------------------------------------------------------------- plain words
def test_a_rule_reads_as_a_sentence_with_resolved_names(st):
    p = fi.plain(st, pol(st, "u1"))
    assert p["key"] == "fw.plain.allow"
    src, dst, svc = p["vars"]["src"], p["vars"]["dst"], p["vars"]["service"]
    assert src["vars"]["what"]["vars"]["names"] == "Cameras"              # the network id became its name
    assert dst["vars"]["what"]["vars"]["items"] == "192.168.50.10"
    assert svc == {"key": "fw.plain.service", "vars": {"proto": "TCP", "ports": "554,7447"}}
    assert [n["key"] for n in p["notes"]] == ["fw.plain.note.return"]


def test_notes_for_states_schedule_off_and_builtin(st):
    p = fi.plain(st, pol(st, "b_invalid"))
    assert {n["key"] for n in p["notes"]} == {"fw.plain.note.states", "fw.plain.note.builtin"}
    assert "fw.plain.note.off" in {n["key"] for n in fi.plain(st, pol(st, "u6"))["notes"]}


def test_every_plain_key_exists_in_the_texts(st):
    seen = set()
    for p in st["policies"]:
        seen |= keys_of(fi.plain(st, p))
    assert seen and not [k for k in seen if k not in I18N]


# ---------------------------------------------------------------------- may A reach B
def test_a_named_rule_decides(st):
    a = fi.ask(st, "n_guest", "n_main")
    assert a["verdict"] == "block" and a["rule"]["name"] == "Guests: no local networks" and a["certain"]


def test_ports_and_addresses_narrow_a_rule(st):
    ok = fi.ask(st, "n_cams", "192.168.50.10", "tcp", 554)
    assert ok["verdict"] == "allow" and ok["rule"]["name"] == "Cameras to recorder only"
    no = fi.ask(st, "n_cams", "192.168.50.10", "tcp", 22)                  # another port: the built-in rule of the pair decides
    assert no["verdict"] == "block" and no["rule"]["builtin"] is True
    other = fi.ask(st, "n_cams", "192.168.50.20", "tcp", 554)              # another address
    assert other["verdict"] == "block" and other["rule"] is not None and other["rule"]["builtin"] is True


def test_without_a_port_a_port_rule_makes_the_answer_uncertain(st):
    a = fi.ask(st, "n_cams", "192.168.50.10")
    assert a["certain"] is False and a["verdict"] == "depends" and a["maybe"][0]["name"] == "Cameras to recorder only"


def test_the_same_network_is_allowed_and_names_resolve(st):
    a = fi.ask(st, "192.168.50.20", "192.168.50.10")
    assert a["verdict"] == "allow" and a["reason"]["key"] == "fw.ask.same_network"
    dev = fi.ask(st, "d08", "d05")
    assert dev["src"]["label"] == "Office laptop" and dev["dst"]["label"] == "Storage" and dev["verdict"] == "allow"


def test_the_internet_is_a_side_and_outside_addresses_count_as_it(st):
    a = fi.ask(st, "n_main", "internet", "tcp", 443)
    assert a["verdict"] == "allow" and a["dst"]["internet"] is True
    b = fi.ask(st, "203.0.113.9", "n_main", "tcp", 443)
    assert b["verdict"] in ("block", "depends")                            # inbound: blocked, the region rule may matter
    assert b["maybe"] and b["maybe"][0]["name"] == "Block countries"


def test_unknown_names_are_an_error_not_a_guess(st):
    assert fi.ask(st, "nope", "n_main") == {"error": "unknown"}
    assert fi.ask(st, "", "n_main") == {"error": "unknown"}


def test_a_switched_off_rule_is_ignored(st):
    assert fi.ask(st, "n_work", "n_cams")["rule"]["name"] != "Old test rule"


def test_states_only_rules_do_not_decide_a_new_connection(st):
    a = fi.ask(st, "n_main", "internet", "tcp", 443)
    assert a["rule"] is None or a["rule"]["id"] != "b_invalid"


def test_port_lists_and_ranges():
    assert fi._port_in("80,443,8000-8100", 8050) is True
    assert fi._port_in("80,443", 22) is False
    assert fi._port_in("group-name", 22) is None
    assert fi._ip_in("10.0.0.5", ["10.0.0.0/24"]) is True
    assert fi._ip_in("10.0.1.5", ["10.0.0.0/24", "10.0.0.9-10.0.0.20"]) is False
    assert fi._ip_in("10.0.1.5", ["some-group"]) is None


def test_all_answer_keys_exist_in_the_texts(st):
    seen = set()
    for a, b, pr, po in (("n_guest", "n_main", None, None), ("n_cams", "192.168.50.10", "tcp", 554), ("n_cams", "192.168.50.10", None, None),
                         ("n_main", "internet", "tcp", 443), ("d08", "d05", None, None), ("203.0.113.9", "n_main", None, None)):
        r = fi.ask(st, a, b, pr, po)
        seen |= keys_of(r["verdict_label"]) | keys_of(r["reason"])
        for m in r["maybe"]:
            seen |= keys_of(m["why"]) | keys_of(m["plain"])
        if r.get("rule"):
            seen |= keys_of(r["rule"]["plain"])
    assert seen and not [k for k in seen if k not in I18N]


# ---------------------------------------------------------------------- checks
def test_checks_carry_a_suggestion_and_the_ids_list_is_flagged(st):
    f = {x["kind"]: x for x in fi.findings(st)}
    assert f["ids_suppression_long"]["params"] == {"n": 37, "limit": fi.IDS_LONG}
    assert f["ids_suppression_long"]["suggestion"] in I18N
    st["ids"] = {"suppressions": 5}
    assert "ids_suppression_long" not in {x["kind"] for x in fi.findings(st)}


def test_a_rule_that_an_earlier_rule_covers_is_shadowed(st):
    st["policies"].append({"id": "x1", "name": "Allow everything from work", "src": {"zone": "z_work"}, "dst": {"zone": "*"}, "action": "allow",
                           "proto": "any", "ports": "", "enabled": True, "order": 5, "builtin": False, "states": [], "scheduled": False})
    st["policies"].append({"id": "x2", "name": "Work to cameras only", "src": {"zone": "z_work"}, "dst": {"zone": "z_cameras"}, "action": "block",
                           "proto": "any", "ports": "", "enabled": True, "order": 6, "builtin": False, "states": [], "scheduled": False})
    f = [x for x in fi.findings(st) if x["kind"] == "shadowed"]
    one = next(x for x in f if x["subject"] == "x2")
    assert one["params"]["by"] == "Allow everything from work" and one["params"]["same"] is False
    assert "u1" not in {x["subject"] for x in f}                              # a rule from another zone is not covered


def test_a_guest_zone_that_reaches_the_inside_is_an_alert(st):
    for z in st["zones"]:
        if z["id"] == "z_guests":
            z["system"] = "hotspot"
    st["policies"].append({"id": "g1", "name": "Guests everywhere", "src": {"zone": "z_guests"}, "dst": {"zone": "z_internal"}, "action": "allow",
                           "proto": "any", "ports": "", "enabled": True, "order": 7, "builtin": False, "states": [], "scheduled": False})
    f = [x for x in fi.findings(st) if x["kind"] == "guest_reaches_inside"]
    assert f and f[0]["level"] == "alert" and f[0]["params"]["from"] == "Guests"


def test_every_check_text_and_suggestion_exists(st):
    for k in fi.SUGGEST.values():
        assert k in I18N
    for kind in fi.SUGGEST:
        assert "fwr.finding." + kind in I18N, kind


def test_findings_are_sorted_worst_first(st):
    st["findings"] = [{"id": "a", "kind": "rules_disabled", "level": "info", "subject": None, "params": {"n": 1}},
                      {"id": "b", "kind": "admin_on_wan", "level": "alert", "subject": "z", "params": {"zone": "x", "services": "SSH"}}]
    levels = [f["level"] for f in fi.findings(st)]
    assert levels == sorted(levels, key=["alert", "warn", "info"].index)


# ---------------------------------------------------------------------- hit log
def day_start(t):
    import time
    lt = time.localtime(t)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def test_blocked_today_counts_from_the_first_reading_of_the_day():
    log = fi.HitLog(Store(":memory:"))
    t0 = day_start(1_800_000_000) + 3600 * 6
    pols = lambda t: [{"id": "a", "name": "Block X", "action": "block", "hits": int(100 + (t - t0) / 10)},
                      {"id": "b", "name": "Allow Y", "action": "allow", "hits": int(5000 + (t - t0))}]
    for k in range(0, 13):
        log.feed("int:1", pols(t0 + k * 300), t0 + k * 300)
    now = t0 + 12 * 300
    s = log.summary("int:1", pols(now), now)
    assert s["blocked_today"] == 360 and s["top"] == [{"id": "a", "name": "Block X", "n": 360}]
    assert s["hour_ok"] is False or s["hour"]["a"] > 0
    assert s["day"]["b"] == 3600


def test_the_last_hour_needs_half_an_hour_of_history_and_a_restart_keeps_the_day():
    store = Store(":memory:")
    log = fi.HitLog(store)
    t0 = day_start(1_800_000_000) + 3600 * 5
    pols = lambda t: [{"id": "a", "name": "R", "action": "block", "hits": int((t - t0) / 5)}]
    log.feed("k", pols(t0), t0)
    assert log.summary("k", pols(t0 + 600), t0 + 600)["hour_ok"] is False
    for k in range(1, 13):
        log.feed("k", pols(t0 + k * 300), t0 + k * 300)
    now = t0 + 12 * 300
    again = fi.HitLog(store)                                                  # a new object reads what the first one saved
    s = again.summary("k", pols(now), now)
    assert s["hour_ok"] is True and s["hour"]["a"] == 720 and s["blocked_today"] == 720


def test_a_counter_that_started_over_counts_from_zero():
    log = fi.HitLog(Store(":memory:"))
    t0 = day_start(1_800_000_000) + 3600 * 5
    log.feed("k", [{"id": "a", "name": "R", "action": "block", "hits": 1000}], t0)
    log.feed("k", [{"id": "a", "name": "R", "action": "block", "hits": 40}], t0 + 400)
    assert log.summary("k", [{"id": "a", "name": "R", "action": "block", "hits": 40}], t0 + 400)["blocked_today"] == 40


def test_policies_without_counters_make_no_numbers():
    log = fi.HitLog(Store(":memory:"))
    pols = [{"id": "a", "name": "R", "action": "block", "hits": None}]
    log.feed("k", pols, 1.0)
    assert log.summary("k", pols, 2.0)["blocked_today"] is None


def test_a_new_day_starts_a_new_count():
    log = fi.HitLog(Store(":memory:"))
    t0 = day_start(1_800_000_000) + 3600 * 20
    log.feed("k", [{"id": "a", "name": "R", "action": "block", "hits": 10}], t0)
    t1 = t0 + 6 * 3600                                                          # after midnight
    log.feed("k", [{"id": "a", "name": "R", "action": "block", "hits": 500}], t1)
    s = log.summary("k", [{"id": "a", "name": "R", "action": "block", "hits": 520}], t1 + 60)
    assert s["blocked_today"] == 20


def test_the_demo_counters_grow_with_time():
    assert policy_hits("u5", 77310, 1_800_000_000) < policy_hits("u5", 77310, 1_800_003_600)


# ---------------------------------------------------------------------- board
def test_the_board_lists_pairs_that_may_talk_or_have_rules_of_their_own(st):
    b = fi.board(st, {"u1": 500, "u3": 30, "u5": 2000})
    pairs = {(p["from"], p["to"]): p for p in b["pairs"]}
    assert len(b["zones"]) == len(st["zones"])
    assert pairs[("z_cameras", "z_internal")]["own"] == 1 and pairs[("z_cameras", "z_internal")]["hits"] == 500
    assert pairs[("z_guests", "z_internal")]["blocked"] == 30
    assert pairs[("z_external", "z_internal")]["blocked"] == 2000 and pairs[("z_external", "z_internal")]["allow"] is False
    assert all(p["from"] != p["to"] for p in b["pairs"])
