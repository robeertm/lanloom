"""Internet watch (lanloom/controlroom/wan.py): two axes, flap guard, reserve line, history, probe settings and the probe itself.

A fixed clock throughout; the probe runs only against a server on 127.0.0.1 or a closed port there."""
import asyncio
import socket

import pytest

from crhelp import T0, FakeCore, gw_entry, line
from lanloom.controlroom import wan as wan_mod
from lanloom.controlroom.wan import WanWatch


def make():
    core = FakeCore()
    return core, WanWatch(core, clock=lambda: T0)


def lines_up(primary=True, reserve=None):
    out = [line("p", up=primary, role="primary", name="Fibre")]
    if reserve is not None:
        out.append(line("b", up=reserve, role="backup", name="Mobile"))
    return out


def set_gateway(core, wans, age=0):
    core.configs = [gw_entry(wans, age)] if wans is not None else []


def probe_on(w, **cfg):
    w.set_probe(dict({"tcp": ["192.0.2.53:53"]}, **cfg))


def probe_result(w, ok, now):
    w.feed_probe({"ok": ok, "ms": 12.0 if ok else None, "dns_ok": None, "tcp_ok": ok}, now)


# ---------------------------------------------------------------------------------------------------- axis 1: the gateway
def test_gateway_axis_from_wans_any_enabled_line_up_counts():
    core, w = make()
    set_gateway(core, [line("a", up=False, role="primary"), line("b", up=True, role="backup")])
    assert w.gateway_axis(T0)["state"] == "up" and w.gateway_axis(T0)["from"] == "wans"
    set_gateway(core, [line("a", up=False, role="primary"), line("b", up=False, role="backup")])
    assert w.gateway_axis(T0)["state"] == "down"
    set_gateway(core, [line("a", up=False, role="primary"), line("b", up=True, role="backup", enabled=False)])
    assert w.gateway_axis(T0)["state"] == "down"                      # a switched-off line does not carry the internet
    set_gateway(core, [line("a", role="primary"), line("b", role="backup")])      # lines that do not tell their state
    assert w.gateway_axis(T0) == {"state": "unknown", "age_s": None, "from": ""}


def test_gateway_axis_age_is_the_newest_configuration():
    core, w = make()
    core.configs = [gw_entry([line("a", up=True)], age=500, key="int:1"), gw_entry([line("b", up=True)], age=40, key="int:2")]
    lines, age = w.gateway_lines(T0)
    assert age == 40 and len(lines) == 2


def test_gateway_lines_older_than_the_window_are_not_used():
    core, w = make()
    set_gateway(core, [line("a", up=True)], age=wan_mod.GATEWAY_FRESH_S + 1)
    assert w.gateway_axis(T0)["state"] == "unknown"


def test_lines_are_normalised_and_hostile_values_do_not_break_them():
    core, w = make()
    set_gateway(core, [{"id": "x", "up": "yes", "active": 1, "role": "weird", "kind": "satellite", "rx_bps": "nan", "name": "n" * 200}])
    ln = w.gateway_lines(T0)[0][0]
    assert ln["up"] is None and ln["active"] is None and ln["role"] == "single" and ln["kind"] == "other"
    assert ln["rx_bps"] is None and len(ln["name"]) == 64 and ln["enabled"] is True


def test_a_line_with_only_a_name_is_dropped_next_to_lines_that_tell_their_state():
    core, w = make()
    set_gateway(core, [line("a", up=True, role="primary"), line("ghost")])
    assert [ln["id"] for ln in w.gateway_lines(T0)[0]] == ["a"]
    set_gateway(core, [line("ghost")])
    assert [ln["id"] for ln in w.gateway_lines(T0)[0]] == ["ghost"]     # alone it stays: nothing else to say


def test_two_unlabelled_lines_become_primary_and_reserve():
    core, w = make()
    set_gateway(core, [line("a", up=True), line("b", up=True)])
    assert [ln["role"] for ln in w.gateway_lines(T0)[0]] == ["primary", "backup"]
    set_gateway(core, [line("a", up=True)])
    assert w.gateway_lines(T0)[0][0]["role"] == "single"


def test_snmp_uplink_is_the_gateway_axis_without_wans():
    core, w = make()
    core.gateway_ip = "192.168.50.1"
    core.snmp = {1: {"host": "192.168.50.1", "t": T0 - 20, "ifaces": [{"name": "eth0", "oper": "up", "in": 5, "out": 5},
                                                                       {"name": "wan0", "oper": "down", "in": 0, "out": 0}]}}
    ax = w.gateway_axis(T0)
    assert ax["state"] == "down" and ax["from"] == "uplink" and ax["age_s"] == 20            # name "wan" wins over traffic
    core.snmp[1]["ifaces"][1]["oper"] = "up"
    assert w.gateway_axis(T0)["state"] == "up"
    core.snmp[1]["ifaces"][1]["oper"] = "unknown"
    assert w.gateway_axis(T0)["state"] == "unknown"
    core.snmp = {1: {"host": "192.168.50.77", "t": T0, "ifaces": [{"name": "wan0", "oper": "up"}]}}       # a different device
    assert w.gateway_axis(T0)["state"] == "unknown"


def test_wans_win_over_snmp():
    core, w = make()
    core.gateway_ip = "192.168.50.1"
    core.snmp = {1: {"host": "192.168.50.1", "t": T0, "ifaces": [{"name": "wan0", "oper": "up"}]}}
    set_gateway(core, [line("a", up=False)])
    assert w.gateway_axis(T0)["from"] == "wans" and w.gateway_axis(T0)["state"] == "down"


# ---------------------------------------------------------------------------------------------------- the decision
def test_measurement_beats_the_gateway_in_both_directions():
    core, w = make()
    probe_on(w)
    set_gateway(core, lines_up(primary=False))
    probe_result(w, True, T0)
    r = w.step(T0)
    assert r["raw"] == "up" and w.basis == "measured" and w.disagree == "gateway_down_measured_up"
    set_gateway(core, lines_up(primary=True))
    probe_result(w, False, T0 + 15)
    r = w.step(T0 + 15)
    assert r["raw"] == "down" and w.basis == "measured" and w.disagree == "gateway_up_measured_down"
    probe_result(w, False, T0 + 30)
    set_gateway(core, lines_up(primary=False))
    w.step(T0 + 30)
    assert w.basis == "both" and w.disagree == "none"


def test_an_unknown_gateway_is_no_disagreement():
    core, w = make()
    probe_on(w)
    probe_result(w, True, T0)
    w.step(T0)
    assert w.basis == "measured" and w.disagree == "none"


def test_a_stale_probe_does_not_count_as_a_measurement():
    core, w = make()
    probe_on(w)
    set_gateway(core, lines_up(primary=False))
    probe_result(w, True, T0)
    w.step(T0 + wan_mod.PROBE_FRESH_S + 1)
    assert w.basis == "gateway" and w._streak["state"] == "down"
    ax = w._probe_axis(T0 + wan_mod.PROBE_FRESH_S + 1)
    assert ax["state"] == "unknown" and ax["age_s"] > wan_mod.PROBE_FRESH_S


def test_a_disabled_probe_is_off_and_its_old_result_is_ignored():
    core, w = make()
    probe_on(w)
    probe_result(w, True, T0)
    w.set_probe({"enabled": False, "tcp": ["192.0.2.53:53"]})
    assert w._probe_axis(T0)["state"] == "off"
    set_gateway(core, lines_up(primary=False))
    w.step(T0)
    assert w.basis == "gateway"


def test_nothing_known_nothing_claimed():
    core, w = make()
    w.step(T0)
    assert w.basis == "none" and w.confirmed is None
    v = w.view(T0)
    assert v["state"] == "unknown" and v["level"] == "unknown"


# ---------------------------------------------------------------------------------------------------- flap guard
def run(w, core, seq, t0=T0, step_s=15.0):
    """seq: gateway state per step (True up, False down); returns the time of the last step."""
    t = t0
    for up in seq:
        set_gateway(core, lines_up(primary=up))
        w.step(t)
        t += step_s
    return t - step_s


def kinds(w):
    return [r["kind"] for r in w.store.query("SELECT kind FROM wan_log ORDER BY t, rowid")]


def test_down_counts_after_three_results_up_after_two():
    core, w = make()
    run(w, core, [True])
    assert w.confirmed == "up" and kinds(w) == []                     # first knowledge: nothing to log
    run(w, core, [False, False], T0 + 15)
    assert w.confirmed == "up"
    run(w, core, [False], T0 + 45)
    assert w.confirmed == "down" and kinds(w) == ["down"] and w.since == T0 + 45
    run(w, core, [True], T0 + 60)
    assert w.confirmed == "down"
    run(w, core, [True], T0 + 75)
    assert w.confirmed == "up" and kinds(w) == ["down", "up"]


def test_a_single_blip_changes_nothing():
    core, w = make()
    run(w, core, [True, True, False, True, True, False, True])
    assert w.confirmed == "up" and kinds(w) == []


def test_four_changes_in_ten_minutes_are_flapping_with_one_log_line():
    core, w = make()
    last = run(w, core, [True] + [False] * 3 + [True] * 2 + [False] * 3 + [True] * 2)          
    assert kinds(w) == ["down", "up", "down", "flapping"]
    assert w.flapping_since is not None
    n = len(kinds(w))
    last = run(w, core, [True] * 2 + [False] * 3 + [True] * 2 + [False] * 3, last + 15)           # more changes while flapping
    assert len(kinds(w)) == n                                                                   # still one line, not twenty
    assert w.view(last)["state"] == "flapping" and w.view(last)["level"] == "warn"


def test_flapping_ends_after_ten_quiet_minutes_with_one_steady_line():
    core, w = make()
    last = run(w, core, [True] + [False] * 3 + [True] * 2 + [False] * 3 + [True] * 2)
    assert w.flapping_since is not None
    t = last
    set_gateway(core, lines_up(primary=True))
    while t < last + wan_mod.FLAP_QUIET_S - 30:
        t += 15
        w.step(t)
    assert w.flapping_since is not None                               # not quiet long enough yet
    t += 60
    w.step(t)
    t += 15
    w.step(t)
    assert w.flapping_since is None and kinds(w).count("steady") == 1 and kinds(w).count("flapping") == 1
    assert w.view(t)["state"] == "up"


def test_flapping_that_settles_down_leaves_the_outage_in_the_log():
    """The line flapped and the last state is down: the history must say down again, or the outage is lost after the steady line."""
    core, w = make()
    last = run(w, core, [True] + [False] * 3 + [True] * 2 + [False] * 3 + [True] * 2 + [False] * 3)
    assert w.confirmed == "down" and w.flapping_since is not None
    t = last
    while w.flapping_since is not None and t < last + 2000:
        t += 15
        w.step(t)
    assert w.flapping_since is None and w.confirmed == "down"
    eps = w.episodes(T0 - 1, t)
    assert eps[-1]["kind"] == "down" and eps[-1]["open"] is True


def test_unknown_resets_the_streak():
    core, w = make()
    run(w, core, [True])
    run(w, core, [False, False], T0 + 15)
    set_gateway(core, None)
    w.step(T0 + 45)
    run(w, core, [False, False], T0 + 60)
    assert w.confirmed == "up"                                        # two plus two is not three in a row


def test_state_is_unknown_instead_of_old_after_five_minutes_without_a_fresh_axis():
    core, w = make()
    run(w, core, [True, True])
    assert w.view(T0 + 15)["state"] == "up"
    set_gateway(core, None)
    w.step(T0 + 30)
    assert w.view(T0 + 30 + wan_mod.UNKNOWN_AFTER_S - 1)["state"] == "up"
    v = w.view(T0 + 30 + wan_mod.UNKNOWN_AFTER_S + 1)
    assert v["state"] == "unknown" and v["level"] == "unknown"
    w.step(T0 + 30 + wan_mod.UNKNOWN_AFTER_S + 1)
    slot = int((T0 + 30 + wan_mod.UNKNOWN_AFTER_S + 1) // wan_mod.SLOT_S)
    assert w.store.one("SELECT state FROM wan_slots WHERE slot=?", (slot,))["state"] in ("?", "u")


def test_a_fresh_axis_after_the_blind_phase_brings_the_state_back():
    core, w = make()
    run(w, core, [True, True])
    set_gateway(core, None)
    w.step(T0 + 30)
    w.step(T0 + 30 + wan_mod.UNKNOWN_AFTER_S + 5)
    set_gateway(core, lines_up(primary=True))
    w.step(T0 + 400)
    assert w.view(T0 + 400)["state"] == "up"


# ---------------------------------------------------------------------------------------------------- reserve line
def failover_lines(primary_active=None, reserve_active=None, primary_up=None, reserve_up=None):
    return [line("p", up=primary_up, active=primary_active, role="primary", name="Fibre"),
            line("b", up=reserve_up, active=reserve_active, role="backup", name="Mobile")]


def test_failover_and_failback_from_the_active_flags():
    core, w = make()
    set_gateway(core, failover_lines(primary_active=True, reserve_active=False, primary_up=True, reserve_up=True))
    w.step(T0)
    assert w._on_reserve is False and kinds(w) == []
    set_gateway(core, failover_lines(primary_active=False, reserve_active=True, primary_up=False, reserve_up=True))
    w.step(T0 + 15)
    assert w._on_reserve is True and w.reserve_since == T0 + 15 and kinds(w) == ["failover"]
    row = w.store.one("SELECT line FROM wan_log WHERE kind='failover'")
    assert row["line"] == "Mobile"
    w.step(T0 + 30)                                                   # still on the reserve: no second line
    assert kinds(w) == ["failover"]
    set_gateway(core, failover_lines(primary_active=True, reserve_active=False, primary_up=True, reserve_up=True))
    w.step(T0 + 45)
    assert w._on_reserve is False and w.reserve_since is None and kinds(w) == ["failover", "failback"]
    assert w.store.one("SELECT line FROM wan_log WHERE kind='failback'")["line"] == "Fibre"
    assert w.failovers_24h(T0 + 45) == 1 and w.failovers_24h(T0 + 86400 + 100) == 0


def test_failover_from_the_up_flags_when_no_line_tells_it_is_active():
    core, w = make()
    set_gateway(core, failover_lines(primary_up=True, reserve_up=True))
    w.step(T0)
    assert w._on_reserve is False
    set_gateway(core, failover_lines(primary_up=False, reserve_up=True))
    w.step(T0 + 15)
    assert w._on_reserve is True and kinds(w) == ["failover"]
    set_gateway(core, failover_lines(primary_up=True, reserve_up=True))
    w.step(T0 + 30)
    assert w._on_reserve is False and kinds(w) == ["failover", "failback"]


def test_both_lines_down_is_no_reserve_news():
    core, w = make()
    set_gateway(core, failover_lines(primary_up=True, reserve_up=True))
    w.step(T0)
    set_gateway(core, failover_lines(primary_up=False, reserve_up=False))
    w.step(T0 + 15)
    assert w._on_reserve is False and kinds(w) == []                 # nothing carries traffic: not "on the reserve"


def test_no_reserve_line_no_failover_state():
    core, w = make()
    set_gateway(core, lines_up(primary=True))
    w.step(T0)
    assert w._on_reserve is None and w.view(T0)["failover"] == {"has_reserve": False, "on_reserve": False, "since_t": None, "switches_24h": 0}


def test_a_disabled_reserve_line_is_no_reserve():
    core, w = make()
    set_gateway(core, [line("p", up=True, role="primary"), line("b", up=True, role="backup", enabled=False)])
    w.step(T0)
    assert w._on_reserve is None


def test_view_is_warn_while_the_reserve_carries_the_internet():
    core, w = make()
    set_gateway(core, failover_lines(primary_up=False, reserve_up=True, primary_active=False, reserve_active=True))
    for i in range(3):
        w.step(T0 + 15 * i)
    v = w.view(T0 + 45)
    assert v["state"] == "up" and v["level"] == "warn" and v["failover"]["on_reserve"] is True
    assert v["failover"]["has_reserve"] is True


def test_switches_24h_counts_failovers_only():
    core, w = make()
    for i, k in enumerate(("failover", "failback", "failover", "down")):
        w._log(T0 - 100 * (i + 1), k, "", "", "")
    w._log(T0 - 90000, "failover", "", "", "")
    assert w.failovers_24h(T0) == 2


# ---------------------------------------------------------------------------------------------------- history
def put_slots(w, cur, states):
    for i, s in enumerate(states):
        w.store.execute("INSERT OR REPLACE INTO wan_slots(slot, state) VALUES (?,?)", (cur - len(states) + 1 + i, s))


def test_slot_worst_state_wins_inside_a_slot():
    core, w = make()
    set_gateway(core, lines_up(primary=True))
    w.step(T0)
    slot = int(T0 // wan_mod.SLOT_S)
    assert w.store.one("SELECT state FROM wan_slots WHERE slot=?", (slot,))["state"] == "u"
    w.confirmed = "down"
    w._slot(T0 + 100)
    assert w.store.one("SELECT state FROM wan_slots WHERE slot=?", (slot,))["state"] == "d"
    w.confirmed = "up"
    w._slot(T0 + 200)
    assert w.store.one("SELECT state FROM wan_slots WHERE slot=?", (slot,))["state"] == "d"       # a later calm moment does not hide it
    w._slot(T0 + wan_mod.SLOT_S + 1)
    assert w.store.one("SELECT state FROM wan_slots WHERE slot=?", (slot + 1,))["state"] == "u"


def test_slot_letters_reserve_flapping_unknown():
    core, w = make()
    w.confirmed, w._on_reserve = "up", True
    w._slot(T0)
    assert w.store.one("SELECT state FROM wan_slots")["state"] == "r"
    w.store.execute("DELETE FROM wan_slots")
    w.flapping_since = T0
    w._slot(T0)
    assert w.store.one("SELECT state FROM wan_slots")["state"] == "f"
    w.store.execute("DELETE FROM wan_slots")
    w.confirmed = None
    w._slot(T0)
    assert w.store.one("SELECT state FROM wan_slots")["state"] == "?"


def test_old_slots_and_log_lines_are_pruned_after_35_days():
    core, w = make()
    old = int((T0 - 40 * 86400) // wan_mod.SLOT_S)
    w.store.execute("INSERT INTO wan_slots(slot, state) VALUES (?, 'u')", (old,))
    w._log(T0 - 40 * 86400, "down", "", "", "")
    w._log(T0 - 86400, "down", "", "", "")
    w.confirmed = "up"
    w._slot(T0)
    assert w.store.one("SELECT COUNT(*) AS n FROM wan_slots WHERE slot=?", (old,))["n"] == 0
    assert w.store.one("SELECT COUNT(*) AS n FROM wan_log")["n"] == 1


def test_history_bar_uptime_and_events():
    core, w = make()
    cur = int(T0 // wan_mod.SLOT_S)
    put_slots(w, cur, ["u"] * 90 + ["d"] * 2 + ["r"] * 2 + ["u"] * 2)             # 96 slots = 24 h
    h = w.history(T0)
    assert len(h["bar"]) == 96 and h["bar"] == "u" * 90 + "dd" + "rr" + "uu"
    assert h["uptime_pct_7d"] == round(100.0 * 94 / 96, 2)                      # reserve counts as up
    assert h["start_t"] == (cur - 95) * wan_mod.SLOT_S and h["slot_s"] == 900


def test_history_unknown_slots_are_no_uptime_and_an_empty_week_has_no_percentage():
    core, w = make()
    assert w.history(T0)["uptime_pct_7d"] is None and w.history(T0)["bar"] == "?" * 96
    cur = int(T0 // wan_mod.SLOT_S)
    put_slots(w, cur, ["?"] * 10 + ["u"] * 10)
    assert w.history(T0)["uptime_pct_7d"] == 100.0                              # slots without a state are not counted at all
    put_slots(w, cur, ["d"] * 10 + ["u"] * 10)
    assert w.history(T0)["uptime_pct_7d"] == 50.0


def test_history_events_newest_first_and_outage_count():
    core, w = make()
    w._log(T0 - 5000, "down", "", "measured", "")
    w._log(T0 - 4000, "up", "", "measured", "")
    w._log(T0 - 3000, "down", "", "gateway", "")
    w._log(T0 - 8 * 86400, "down", "", "gateway", "")
    h = w.history(T0)
    assert [e["kind"] for e in h["events"]] == ["down", "up", "down"] and h["outages_7d"] == 2


def test_episodes_closed_open_flapping_and_window():
    core, w = make()
    for t, k in ((100, "down"), (200, "up"), (300, "flapping"), (400, "steady"), (500, "down")):
        w._log(1000 + t, k, "", "", "")
    eps = w.episodes(1000, 1700)
    assert eps == [{"kind": "down", "from_t": 1100, "to_t": 1200, "open": False}, {"kind": "flapping", "from_t": 1300, "to_t": 1400, "open": False},
                   {"kind": "down", "from_t": 1500, "to_t": 1700, "open": True}]
    assert w.episodes(1250, 1700)[0]["from_t"] == 1300                         # an episode that ended before the window is not in it
    inside = w.episodes(1120, 1190)
    assert inside == [{"kind": "down", "from_t": 1120, "to_t": 1190, "open": True}]


def test_a_down_inside_flapping_turns_the_episode_into_an_outage():
    core, w = make()
    for t, k in ((100, "flapping"), (150, "down"), (300, "up")):
        w._log(1000 + t, k, "", "", "")
    assert w.episodes(1000, 2000) == [{"kind": "down", "from_t": 1100, "to_t": 1300, "open": False}]


# ---------------------------------------------------------------------------------------------------- gateway events
def test_gateway_events_are_added_once():
    core, w = make()
    ev = [{"t": 100.0, "kind": "down", "line": "Fibre"}, {"t": 200.0, "kind": "up", "line": "Fibre"}]
    assert w.ingest_gateway_events(ev) == 2
    assert w.ingest_gateway_events(ev + [{"t": 300.0, "kind": "failover", "line": "Mobile"}]) == 1
    assert w.ingest_gateway_events(ev) == 0
    rows = w.store.query("SELECT t, kind, line, basis FROM wan_log ORDER BY t")
    assert [(r["t"], r["kind"], r["basis"]) for r in rows] == [(100.0, "down", "gateway-event"), (200.0, "up", "gateway-event"),
                                                               (300.0, "failover", "gateway-event")]


def test_gateway_events_with_bad_content_are_skipped():
    core, w = make()
    junk = ["x", None, 5, {"t": "soon", "kind": "down"}, {"t": 1.0, "kind": "explode"}, {"kind": "down"}, {"t": float("nan"), "kind": "up"},
            {"t": 5.0, "kind": "down", "line": "L" * 200}]
    assert w.ingest_gateway_events(junk) == 1
    assert len(w.store.one("SELECT line FROM wan_log")["line"]) == 64
    assert w.ingest_gateway_events(None) == 0


# ---------------------------------------------------------------------------------------------------- probe settings
@pytest.mark.parametrize("text,expect", [
    ("1.1.1.1:53", "1.1.1.1:53"), ("Example.ORG", "example.org:443"), ("host:", "host:443"), (" host:8443 ", "host:8443"),
    ("host:1", "host:1"), ("host:65535", "host:65535"), ("host:0", None), ("host:65536", None), ("host:-5", None), ("host:abc", None),
    ("", None), (None, None), ("host name:53", None), ("a/b:80", None), ("user@host:80", None), ("http://host", None), (":53", None),
    ("a" * 254, None), ("host\x00x:53", None), ("[2001:db8::1]:53", "[2001:db8::1]:53"), ("2001:db8::1", "[2001:db8::1]:443"),
    ("[2001:db8::1]", "[2001:db8::1]:443"), ("[not an address]:53", None), ("[2001:db8::1]:0", None),
])
def test_clean_target(text, expect):
    assert wan_mod.clean_target(text) == expect


@pytest.mark.parametrize("text,expect", [
    ("example.org", "example.org"), ("Example.ORG.", "example.org"), ("", ""), (None, ""), ("   ", ""), ("-bad.org", None),
    (".bad.org", None), ("a b.org", None), ("ex_ample.org", None), ("a..b", None), ("a" * 254, None),
    ("host/x", None), ("host:53", None),
])
def test_clean_name(text, expect):
    assert wan_mod.clean_name(text) == expect


def test_clean_probe_enabled_only_with_a_target():
    assert wan_mod.clean_probe({}) == {"enabled": False, "dns": "", "tcp": []}
    assert wan_mod.clean_probe({"enabled": True}) == {"enabled": False, "dns": "", "tcp": []}
    assert wan_mod.clean_probe({"dns": "Example.org"}) == {"enabled": True, "dns": "example.org", "tcp": []}
    assert wan_mod.clean_probe({"dns": "example.org", "enabled": False})["enabled"] is False
    assert wan_mod.clean_probe(None) == {"enabled": False, "dns": "", "tcp": []}


def test_clean_probe_limits_and_errors():
    five = ["192.0.2.%d:53" % i for i in range(1, 6)]
    four = five[:4]
    assert wan_mod.clean_probe({"tcp": four})["tcp"] == four
    with pytest.raises(ValueError, match="wan.err.probe_many"):
        wan_mod.clean_probe({"tcp": five})
    assert wan_mod.clean_probe({"tcp": four + [four[0]]})["tcp"] == four       # a repeated target is no fifth one
    assert wan_mod.clean_probe({"tcp": ["192.0.2.1:53", "192.0.2.1"]})["tcp"] == ["192.0.2.1:53", "192.0.2.1:443"]
    with pytest.raises(ValueError, match="wan.err.probe_target"):
        wan_mod.clean_probe({"tcp": ["192.0.2.1:53", "bad target"]})
    with pytest.raises(ValueError, match="wan.err.probe_name"):
        wan_mod.clean_probe({"dns": "-nope"})
    with pytest.raises(ValueError, match="wan.err.probe_target"):
        wan_mod.clean_probe({"tcp": "192.0.2.1:53"})                          # a text where a list belongs is no list of single letters


def test_probe_setting_roundtrip_and_a_broken_stored_value():
    core, w = make()
    assert w.probe_setting() == {"enabled": False, "dns": "", "tcp": []}
    cfg = w.set_probe({"dns": "example.org", "tcp": ["192.0.2.53:53"]})
    assert cfg == w.probe_setting() == {"enabled": True, "dns": "example.org", "tcp": ["192.0.2.53:53"]}
    core.store.set_setting(wan_mod.SETTING, {"enabled": True, "tcp": ["bad target"]})
    assert w.probe_setting() == {"enabled": False, "dns": "", "tcp": []}
    core.store.set_setting(wan_mod.SETTING, "junk")
    assert w.probe_setting()["enabled"] is False


def test_changing_the_targets_forgets_the_old_measurement():
    core, w = make()
    probe_on(w)
    probe_result(w, True, T0)
    probe_on(w, tcp=["192.0.2.54:53"])
    assert w.probe_state["t"] is None and w._probe_axis(T0)["state"] == "unknown"


def test_feed_probe_counts_failures_in_a_row_and_ignores_not_asked():
    core, w = make()
    probe_on(w)
    probe_result(w, False, T0)
    probe_result(w, False, T0 + 20)
    assert w.probe_state["fails"] == 2
    w.feed_probe({"ok": None}, T0 + 40)
    assert w.probe_state["t"] == T0 + 20
    probe_result(w, True, T0 + 60)
    assert w.probe_state["fails"] == 0 and w.probe_state["ok"] is True


def test_probe_if_due_runs_every_twenty_seconds():
    core = FakeCore()
    clock = {"t": T0}
    w = WanWatch(core, clock=lambda: clock["t"])
    calls = []

    async def fake():
        calls.append(clock["t"])
        return {"ok": True, "ms": 5.0, "dns_ok": None, "tcp_ok": True}
    w.probe_once = fake
    asyncio.run(w.probe_if_due())
    clock["t"] = T0 + 10
    asyncio.run(w.probe_if_due())
    clock["t"] = T0 + 21
    asyncio.run(w.probe_if_due())
    assert calls == [T0, T0 + 21] and w.probe_state["ok"] is True


# ---------------------------------------------------------------------------------------------------- the probe itself
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_no_targets_no_traffic(monkeypatch):
    core, w = make()

    def boom(*a, **k):
        raise AssertionError("the probe opened a connection without targets")
    monkeypatch.setattr(asyncio, "open_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert asyncio.run(w.probe_once()) == {"ok": None}
    w.set_probe({"enabled": True})                                            # enabled without a target stays off
    assert asyncio.run(w.probe_once()) == {"ok": None}
    core.store.set_setting(wan_mod.SETTING, {"enabled": False, "tcp": ["192.0.2.53:53"], "dns": "example.org"})
    assert asyncio.run(w.probe_once()) == {"ok": None}
    asyncio.run(w.probe_if_due())
    assert w.probe_state["t"] is None


def test_probe_against_a_local_server_and_a_closed_port():
    core, w = make()

    async def go():
        opened = []

        async def handler(r, wr):
            opened.append(1)
            wr.close()
        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            w.set_probe({"tcp": ["127.0.0.1:%d" % port]})
            up = await w.probe_once()
            await asyncio.sleep(0.05)
            w.set_probe({"tcp": ["127.0.0.1:%d" % free_port()]})
            down = await w.probe_once()
            w.set_probe({"tcp": ["127.0.0.1:%d" % free_port(), "127.0.0.1:%d" % port]})          # the first fails, the second answers
            second = await w.probe_once()
        finally:
            srv.close()
            await srv.wait_closed()
        return up, down, second, len(opened)
    up, down, second, opened = asyncio.run(go())
    assert up["ok"] is True and up["tcp_ok"] is True and up["dns_ok"] is None and isinstance(up["ms"], float)
    assert down == {"ok": False, "ms": None, "dns_ok": None, "tcp_ok": False}
    assert second["ok"] is True and opened >= 2


def test_probe_name_lookup_and_the_combination(monkeypatch):
    core, w = make()
    answers = {"ok.test": True}

    def fake_getaddrinfo(host, *a, **k):
        if answers.get(host):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 0))]
        raise socket.gaierror("no such name")
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    async def go():
        srv = await asyncio.start_server(lambda r, wr: wr.close(), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            w.set_probe({"dns": "ok.test"})
            only_dns = await w.probe_once()
            w.set_probe({"dns": "gone.test"})
            bad_dns = await w.probe_once()
            w.set_probe({"dns": "ok.test", "tcp": ["127.0.0.1:%d" % port]})
            both = await w.probe_once()
            w.set_probe({"dns": "gone.test", "tcp": ["127.0.0.1:%d" % port]})
            mixed = await w.probe_once()
        finally:
            srv.close()
            await srv.wait_closed()
        return only_dns, bad_dns, both, mixed
    only_dns, bad_dns, both, mixed = asyncio.run(go())
    assert only_dns["ok"] is True and only_dns["dns_ok"] is True and only_dns["tcp_ok"] is None
    assert bad_dns["ok"] is False and bad_dns["dns_ok"] is False and bad_dns["ms"] is None
    assert both["ok"] is True and both["dns_ok"] is True and both["tcp_ok"] is True
    assert mixed["ok"] is False and mixed["dns_ok"] is False and mixed["tcp_ok"] is True        # the lookup alone failing is a failure


def test_probe_gives_up_after_the_timeout(monkeypatch):
    core, w = make()
    import time as _t
    monkeypatch.setattr(wan_mod, "PROBE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_t.sleep(0.6), [])[1])
    w.set_probe({"dns": "slow.test"})
    t0 = _t.monotonic()
    res = asyncio.run(w.probe_once())
    assert res["ok"] is False and res["dns_ok"] is False and _t.monotonic() - t0 < 2.0


def test_ipv6_target_with_brackets_connects():
    core, w = make()
    if not socket.has_ipv6:
        pytest.skip("no IPv6 on this host")

    async def go():
        try:
            srv = await asyncio.start_server(lambda r, wr: wr.close(), "::1", 0)
        except OSError:
            return None
        port = srv.sockets[0].getsockname()[1]
        try:
            w.set_probe({"tcp": ["[::1]:%d" % port]})
            return await w.probe_once()
        finally:
            srv.close()
            await srv.wait_closed()
    res = asyncio.run(go())
    if res is None:
        pytest.skip("no IPv6 loopback")
    assert res["ok"] is True
