"""Rule changes made outside Lanloom (W3): the small state without addresses, the difference (rule new / gone / off / on /
moved / acts differently, zone pair open / closed, port forward, intrusion detection, network in another zone, zone new /
gone, the controller's own rules only as a count), the two-reads rule, the base state without events, the editor's own change
not twice, sites kept apart, the nightly section without doubles, and every sentence in every language."""
import copy
import json
import re
from pathlib import Path

import pytest

from lanloom.firewall import drift, neutral
from lanloom.nightly.journal import Journal
from lanloom.store import Store
from lanloom.textfmt import languages

ROOT = Path(__file__).resolve().parent.parent
T0 = 1_795_000_000.0


def _pol(pid, name, a, b, order, action="allow", **kw):
    return dict({"id": pid, "name": name, "src": {"zone": a}, "dst": {"zone": b}, "action": action, "proto": "any", "ports": "",
                 "return_traffic": False, "enabled": True, "order": order, "builtin": False, "hits": 0}, **kw)


def base_state():
    st = {"controller": {"label": "Test", "kind": "controller"},
          "zones": [{"id": "z_in", "name": "Internal", "system": "internal"}, {"id": "z_net", "name": "Internet", "system": "external"},
                    {"id": "z_lab", "name": "Lab"}, {"id": "z_gw", "name": "Gateway", "system": "gateway"}],
          "networks": [{"id": "n1", "name": "Main", "zone": "z_in", "cidr": "192.0.2.0/24", "vlan": 1, "management": True},
                       {"id": "n2", "name": "Lab net", "zone": "z_lab", "cidr": "198.51.100.0/24", "vlan": 20, "management": False}],
          "devices": [],
          "policies": [_pol("p1", "Main to lab", "z_in", "z_lab", 1),
                       _pol("p2", "Lab to main printer", "z_lab", "z_in", 2, ports="631", proto="tcp",
                            dst={"zone": "z_in", "match": {"kind": "ip", "items": ["192.0.2.40"]}}),
                       _pol("p3", "Block lab", "z_lab", "z_gw", 3, action="block"),
                       _pol("b1", "Allow all traffic", "z_in", "z_net", 9000, builtin=True)],
          "port_forwards": [{"id": "f1", "name": "Photos", "proto": "tcp", "wan_port": "8443", "dst_ip": "192.0.2.10", "dst_port": "443", "enabled": True}],
          "ids": {"enabled": True, "suppressions": 3}}
    st["allow"] = neutral.derive_allow(st, default="block")
    return st


def touch(st):
    """The same configuration with other counters and a new allow list recomputed."""
    st = copy.deepcopy(st)
    st["allow"] = neutral.derive_allow(st, default="block")
    return st


class Rig:
    def __init__(self, own=None):
        self.store = Store(":memory:")
        self.clock = [T0]
        self.journal = Journal(self.store, clock=lambda: self.clock[0])
        self.drift = drift.Drift(self.store, lambda: self.journal, own, clock=lambda: self.clock[0])

    def read(self, state, step=60, key="int:1", name="Gateway", family="fam-a"):
        self.clock[0] += step
        return self.drift.feed(key, name, family, touch(state))

    def settle(self, state, **kw):
        """Two reads in a row with the new state: the change counts with the second one."""
        self.read(state, **kw)
        return self.read(state, **kw)

    def rows(self):
        return self.journal.between(0, self.clock[0] + 1, ("fw_drift",))


def subs(events):
    return [(e["sub"], e.get("name")) for e in events]


# ------------------------------------------------------------------ state and difference
def test_state_has_no_address_and_ignores_counters():
    st = base_state()
    s1 = drift.build_stand(st)
    blob = json.dumps(s1)
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", blob) and "192.0.2" not in blob and "8443" in blob     # the outside port is shown, the target never
    st2 = copy.deepcopy(st)
    for p in st2["policies"]:
        p["hits"] = 12345
    st2["controller"]["t"] = 99
    assert drift.digest(drift.build_stand(st2)) == drift.digest(s1)


def test_names_with_addresses_are_made_unreadable():
    for raw in ("Block 10.0.0.0/24 in", "to fe80::1 now", "dev 02:4c:4c:00:00:01 x", "a<b>c"):
        out = drift.clean_name(raw)
        assert not re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}", out) and "fe80" not in out and "02:4c" not in out and "<" not in out
    assert drift.clean_name("") == "—" and len(drift.clean_name("x" * 200)) <= 61


def test_ids_not_read_is_not_off():
    st = base_state()
    del st["ids"]
    assert drift.build_stand(st)["ids"] is None
    a, b = drift.build_stand(base_state()), drift.build_stand(st)
    assert not [e for e in drift.diff(a, b) if e["sub"] == "ids"]


def _change(fn):
    st = base_state()
    new = fn(copy.deepcopy(st))
    new["allow"] = neutral.derive_allow(new, default="block")
    return drift.diff(drift.build_stand(st), drift.build_stand(new))


def test_rule_new_gone_off_on():
    ev = _change(lambda s: s["policies"].append(_pol("p9", "Visitors", "z_lab", "z_in", 5)) or s)
    assert ("rule_new", "Visitors") in subs(ev) and ev[0]["level"] in ("warn", "danger")
    ev = _change(lambda s: s["policies"].append(_pol("p9", "Visitors", "z_lab", "z_in", 5, enabled=False)) or s)
    assert ("rule_new_off", "Visitors") in subs(ev) and all(e["level"] == "info" for e in ev if e["sub"] == "rule_new_off")
    ev = _change(lambda s: s.__setitem__("policies", [p for p in s["policies"] if p["id"] != "p3"]) or s)
    assert ("rule_gone", "Block lab") in subs(ev)
    ev = _change(lambda s: s["policies"][0].update(enabled=False) or s)
    assert ("rule_off", "Main to lab") in subs(ev)
    st = base_state()
    st["policies"][0]["enabled"] = False
    on = drift.diff(drift.build_stand(st), drift.build_stand(base_state()))
    assert ("rule_on", "Main to lab") in subs(on)


def test_rule_acts_differently_names_the_action_and_ignores_the_name_of_the_effect():
    ev = _change(lambda s: s["policies"][2].update(action="allow") or s)
    e = next(x for x in ev if x["sub"] == "rule_action")
    assert e["action"] == "allow" and e["was"] == "block"
    ev = _change(lambda s: s["policies"][1].update(ports="631,9100") or s)
    assert ("rule_changed", "Lab to main printer") in subs(ev)
    ev = _change(lambda s: s["policies"][1]["dst"]["match"].update(items=["192.0.2.41"]) or s)           # the address inside is only a fingerprint
    assert ("rule_changed", "Lab to main printer") in subs(ev) and "192.0.2" not in json.dumps(ev)
    ev = _change(lambda s: s["policies"][0].update(name="Main to lab (renamed)") or s)
    assert ev == []                                      # a new name alone changes nothing that acts


def test_a_moved_rule_is_one_event_not_all_that_move_down():
    st = base_state()
    for i, nm in enumerate(("Lab to gateway a", "Lab to gateway b", "Lab to gateway log")):
        st["policies"].append(_pol("q%d" % i, nm, "z_lab", "z_gw", 4 + i, action="block"))
    old = drift.build_stand(st)
    new_st = copy.deepcopy(st)
    new_st["policies"][-1]["order"] = 0                      # the last rule of the pair moves to the front
    ev = drift.diff(old, drift.build_stand(new_st))
    moved = [e for e in ev if e["sub"] == "rule_moved"]
    assert [m["name"] for m in moved] == ["Lab to gateway log"]       # one event, although the three before it moved down


def test_zone_pair_open_in_is_danger_and_closed_is_info():
    ev = _change(lambda s: s["policies"].append(_pol("p9", "Any in", "z_net", "z_in", 6)) or s)
    e = next(x for x in ev if x["sub"] == "pair_open_in")
    assert e["level"] == "danger" and e["from"] == "Internet" and e["to"] == "Internal" and ev[0]["level"] == "danger"
    ev = _change(lambda s: s["policies"].append(_pol("p9", "Any gw", "z_net", "z_gw", 6)) or s)
    assert any(x["sub"] == "pair_open_in" for x in ev)               # the gateway counts as inside
    ev = _change(lambda s: s["policies"].append(_pol("p9", "Lab out", "z_lab", "z_net", 6)) or s)
    e = next(x for x in ev if x["sub"] == "pair_open")
    assert e["level"] == "warn"
    st = base_state()
    st["policies"][0]["enabled"] = False
    closed = drift.diff(drift.build_stand(base_state()), drift.build_stand(touch(st)))
    assert any(x["sub"] == "pair_closed" and x["level"] == "info" for x in closed)
    part = _change(lambda s: s["policies"].append(_pol("p9", "Web in", "z_net", "z_in", 6, ports="443", proto="tcp")) or s)
    assert any(x["sub"] == "pair_partial" and x["level"] == "danger" for x in part)


def test_port_forwards_and_ids_and_zones():
    ev = _change(lambda s: s["port_forwards"].append({"id": "f2", "name": "Game", "wan_port": "27015", "dst_ip": "192.0.2.21", "enabled": True}) or s)
    assert ("fwd_new", "Game") in subs(ev) and "192.0.2.21" not in json.dumps(ev)
    ev = _change(lambda s: s["port_forwards"][0].update(enabled=False) or s)
    assert ("fwd_off", "Photos") in subs(ev)
    ev = _change(lambda s: s["port_forwards"][0].update(wan_port="9443") or s)
    e = next(x for x in ev if x["sub"] == "fwd_port")
    assert e["port"] == "9443" and e["was"] == "8443"
    ev = _change(lambda s: s["port_forwards"].clear() or s)
    assert ("fwd_gone", "Photos") in subs(ev)
    ev = _change(lambda s: s["ids"].update(enabled=False) or s)
    e = next(x for x in ev if x["sub"] == "ids")
    assert e["mode"] == "off" and e["was"] == "on" and e["level"] == "warn"
    ev = _change(lambda s: s["networks"][1].update(zone="z_in") or s)
    assert any(x["sub"] == "net_zone" and x["zone"] == "Internal" and x["was"] == "Lab" for x in ev)
    ev = _change(lambda s: s["zones"].append({"id": "z_new", "name": "Guests"}) or s)
    assert ("zone_new", "Guests") in subs(ev)
    ev = _change(lambda s: s.__setitem__("zones", [z for z in s["zones"] if z["id"] != "z_lab"]) or s)
    assert ("zone_gone", "Lab") in subs(ev)


def test_the_controllers_own_rules_count_only():
    ev = _change(lambda s: s["policies"][3].update(action="block") or s)
    assert [e["sub"] for e in ev if e["sub"].startswith("rule_")] == []
    assert any(e["sub"] == "auto" and e["n"] == 1 for e in ev)


def test_too_many_changes_end_in_a_count():
    st = base_state()
    new = copy.deepcopy(st)
    for i in range(80):
        new["policies"].append(_pol("x%d" % i, "Rule %d" % i, "z_lab", "z_in", 100 + i, enabled=False))
    ev = drift.diff(drift.build_stand(st), drift.build_stand(new))
    assert len(ev) == drift.MAX_EVENTS and ev[-1]["sub"] == "bulk" and ev[-1]["n"] > 1


# ------------------------------------------------------------------ the watcher
def test_base_state_gives_no_events_and_a_half_read_state_none():
    r = Rig()
    assert r.read(base_state()) == [] and r.read(base_state()) == []
    half = base_state()
    half["policies"] = half["policies"][:1]              # one read that lost most rules ...
    assert r.read(half) == []
    assert r.read(base_state()) == []                    # ... and the next read is whole again: nothing happened
    assert r.rows() == []


def test_two_equal_reads_make_the_event_with_the_time_of_the_first():
    r = Rig()
    r.read(base_state())
    new = base_state()
    new["policies"].append(_pol("p9", "Visitors", "z_lab", "z_in", 5))
    assert r.read(new) == []
    t_first = r.clock[0]
    ev = r.read(new)
    assert [e["sub"] for e in ev] == ["pair_open", "rule_new"] and ev[1]["name"] == "Visitors"
    row = next(x for x in r.rows() if x["data"]["sub"] == "rule_new")
    assert row["t"] == t_first and row["data"]["controller"] == "Gateway" and row["data"]["family"] == "fam-a" and row["data"]["sub"] == "rule_new"
    assert r.read(new) == [] and len(r.rows()) == 2       # a third read of the same state adds nothing
    assert "192.0.2" not in json.dumps([x["data"] for x in r.rows()])


def test_reads_too_close_together_do_not_count():
    r = Rig()
    r.read(base_state())
    new = base_state()
    new["policies"][0]["enabled"] = False
    r.read(new, step=1)
    assert r.read(new, step=1) == []
    assert ("rule_off", "Main to lab") in subs(r.read(new, step=60))


def test_state_survives_a_restart():
    r = Rig()
    r.read(base_state())
    r2 = drift.Drift(r.store, lambda: r.journal, None, clock=lambda: r.clock[0])
    new = base_state()
    new["port_forwards"][0]["enabled"] = False
    r.clock[0] += 60
    r2.feed("int:1", "Gateway", "fam-a", touch(new))
    r.clock[0] += 60
    assert subs(r2.feed("int:1", "Gateway", "fam-a", touch(new))) == [("fwd_off", "Photos")]


def test_the_editors_own_change_is_not_written_twice():
    state = {"running": False, "done_at": None}

    def own(since, until):
        if since is None:
            return state["running"]
        return state["done_at"] is not None and since - 600 <= state["done_at"] <= until
    r = Rig(own)
    r.read(base_state())
    new = base_state()
    new["policies"][0]["enabled"] = False
    state["running"] = True                                # a plan is being written: the halfway state does not count
    r.read(new)
    assert r.read(new) == [] and r.rows() == []
    state["running"], state["done_at"] = False, r.clock[0]
    assert r.read(new) == [] and r.rows() == []            # finished (fw_change is in the journal): taken over silently


def test_a_later_manual_change_after_an_editor_change_is_reported():
    state = {"done_at": None}
    own = lambda since, until: False if since is None else (state["done_at"] is not None and since - 600 <= state["done_at"] <= until)   # noqa: E731
    r = Rig(own)
    r.read(base_state())
    new = base_state()
    new["policies"][0]["enabled"] = False
    state["done_at"] = r.clock[0] + 30
    r.settle(new)                                          # the editor's change: silent
    assert r.rows() == []
    later = copy.deepcopy(new)
    later["port_forwards"][0]["enabled"] = False
    r.clock[0] += 3600
    ev = r.settle(later)
    assert subs(ev) == [("fwd_off", "Photos")]


def test_sites_are_kept_apart():
    a, b = Rig(), Rig()
    for r in (a, b):
        r.read(base_state())
    new = base_state()
    new["policies"][0]["enabled"] = False
    a.settle(new)
    b.settle(base_state())
    assert len(a.rows()) >= 1 and b.rows() == []


def test_two_controllers_in_one_site_have_their_own_states():
    r = Rig()
    r.read(base_state(), key="int:1", name="Gateway A")
    r.read(base_state(), key="int:2", name="Gateway B")
    new = base_state()
    new["port_forwards"][0]["enabled"] = False
    r.settle(new, key="int:1", name="Gateway A")
    assert [x["data"]["controller"] for x in r.rows()] == ["Gateway A"]
    r.settle(base_state(), key="int:2", name="Gateway B")
    assert len(r.rows()) == 1


def test_a_removed_connection_is_forgotten():
    r = Rig()
    r.read(base_state(), key="int:1")
    r.clock[0] += 60
    r.drift.feed("int:2", "B", "fam-a", touch(base_state()), keep={"int:2"})
    assert "int:1" not in r.store.get_setting(drift.SETTING, {})


# ------------------------------------------------------------------ texts
SUBS = ("pair_open_in", "pair_open", "pair_partial", "pair_closed", "pair_narrowed", "rule_new", "rule_new_off", "rule_gone", "rule_off",
        "rule_on", "rule_moved", "rule_changed", "rule_action", "fwd_new", "fwd_new_off", "fwd_gone", "fwd_off", "fwd_on", "fwd_port", "ids",
        "net_zone", "zone_new", "zone_gone", "auto", "bulk")


def test_every_sentence_exists_in_every_language_and_keeps_its_placeholders():
    tr = {l: json.loads((ROOT / "lanloom" / "i18n" / ("%s.json" % l)).read_text(encoding="utf-8")) for l in languages()}
    assert len(tr) >= 6
    for sub in SUBS:
        en = tr["en"]["fwd.sub." + sub]
        for lang, d in tr.items():
            text = d.get("fwd.sub." + sub)
            assert text, (lang, sub)
            assert set(re.findall(r"\{(\w+)[,}]", text)) == set(re.findall(r"\{(\w+)[,}]", en)), (lang, sub)
    for key in [*("fwd.act." + a for a in (*drift.ACTIONS, "other")), *("fwd.ids." + m for m in drift.IDS_RANK)]:
        for lang, d in tr.items():
            assert d.get(key), (lang, key)


def test_every_event_the_diff_can_make_has_a_sentence():
    tr = json.loads((ROOT / "lanloom" / "i18n" / "en.json").read_text(encoding="utf-8"))
    st = base_state()
    new = copy.deepcopy(st)
    new["policies"].append(_pol("p9", "Any in", "z_net", "z_in", 6))
    new["policies"][0]["enabled"] = False
    new["port_forwards"][0]["wan_port"] = "9443"
    new["ids"]["enabled"] = False
    new["policies"][3]["action"] = "block"
    for e in drift.diff(drift.build_stand(st), drift.build_stand(touch(new))):
        assert drift.describe(e)["key"][len("fwd.sub."):] in SUBS and drift.describe(e)["key"] in tr


# ------------------------------------------------------------------ the app: reads, overview, nightly report
def _core(tmp_path):
    from tests.test_controlroom_core import make
    return make(tmp_path)


def _config_event(state, family="gateway-x"):
    from lanloom.demo_live import gateway_capabilities
    return {"type": "config", "family": family, "state": state, "capabilities": gateway_capabilities(family)}


def test_the_core_compares_every_read_and_the_overview_lists_the_change(tmp_path):
    c = _core(tmp_path)
    integ = c.save_integration("nut", "Gateway", {"host": "192.0.2.10"}, {})
    st = base_state()
    neutral.validate(st)
    for k in range(2):
        c._book_config(integ["id"], _config_event(st), T0 + 60 * k)
    new = touch(st)
    new["port_forwards"][0]["enabled"] = False
    for k in range(3):
        c._book_config(integ["id"], _config_event(new), T0 + 600 + 60 * k)
    rows = c.reports.journal.between(0, T0 + 10_000, ("fw_drift",))
    assert [(r["data"]["sub"], r["data"]["name"]) for r in rows] == [("fwd_off", "Photos")]
    assert rows[0]["data"]["controller"] == "Gateway" and rows[0]["data"]["integ"] == "int:%d" % integ["id"]
    from lanloom import overview
    ch = overview.Overview(c).changes(T0 + 700)
    assert ch and ch[0]["kind"] == "fw_drift" and ch[0]["data"]["sub"] == "fwd_off" and ch[0]["data"]["name"] == "Photos"
    assert "integ" not in ch[0]["data"] and "controller" not in ch[0]["data"] and "family" not in ch[0]["data"]
    other = c.save_integration("nut", "Second", {"host": "192.0.2.11"}, {})
    c.delete_integration(integ["id"])
    c._book_config(other["id"], _config_event(new), T0 + 900)       # the next read of any connection forgets the deleted one
    assert list(c.store.get_setting(drift.SETTING, {})) == ["int:%d" % other["id"]]


def _ctx(rows, since=T0, until=T0 + 100_000):
    store = Store(":memory:")
    j = Journal(store, clock=lambda: until)
    for t, data in rows:
        j.add("fw_drift", data, t)
    from lanloom.nightly.contrib import Ctx

    class _C:
        pass
    return Ctx(core=_C(), journal=j, since=since, until=until, prev=None)


@pytest.mark.parametrize("lang", sorted(languages()))
def test_nightly_section_without_doubles_in_every_language(lang, monkeypatch):
    from datetime import timezone
    from lanloom.firewall import report
    monkeypatch.setattr(report, "own_rule_families", lambda: {"fam-b"})
    from lanloom.nightly.render import Renderer
    rows = [(T0 + 10, dict(sub="pair_open_in", level="danger", integ="int:1", controller="G", family="fam-a", **{"from": "Internet", "to": "Internal"})),
            (T0 + 20, dict(sub="fwd_on", level="warn", integ="int:1", controller="G", family="fam-a", name="Photos", port="8443")),
            (T0 + 30, dict(sub="rule_new", level="warn", integ="int:1", controller="G", family="fam-a", name="Visitors", action="allow", **{"from": "A", "to": "B"})),
            (T0 + 40, dict(sub="rule_new", level="warn", integ="int:2", controller="R", family="fam-b", name="Own", action="allow", **{"from": "A", "to": "B"})),
            (T0 + 50, dict(sub="ids", level="warn", integ="int:1", controller="G", family="fam-a", mode="off", was="on")),
            (T0 + 60, dict(sub="auto", level="info", integ="int:1", controller="G", family="fam-a", n=2))]
    ctx = _ctx(rows)
    assert report._drift_available(ctx)
    sec = report._drift_build(ctx)
    keys = [i["key"] for i in sec["items"]]
    assert "fwd.sub.pair_open_in" not in keys                                # the zone matrix section says it
    assert keys.count("fwd.sub.rule_new") == 1 and not any(i["vars"].get("name") == "Own" for i in sec["items"])   # the family's own section says it
    assert sec["summary"]["key"] == "report.ruledrift.summary_trouble" and sec["summary"]["vars"] == {"n": 4, "bad": 3}
    r = Renderer(lang, timezone.utc, "2026-11-18")
    for text in [r.line(sec["summary"]), r.tr(sec["title"])] + [r.line(i) for i in sec["items"]]:
        assert "{" not in text and "fwd." not in text and "report." not in text, text
    assert any("Photos" in r.line(i) for i in sec["items"])
    assert not report._drift_available(_ctx([]))
    assert not report._drift_available(_ctx(rows[:1]))                      # only a zone pair: left out, "Zone matrix" has it


def test_families_with_their_own_rule_section_are_all_flagged():
    own = sorted(p.parent.name for p in (ROOT / "lanloom" / "integrations").glob("*/report.py") if re.search(r"rule_new", p.read_text(encoding="utf-8")))
    from lanloom.firewall import report
    assert own and own == sorted(report.own_rule_families())
