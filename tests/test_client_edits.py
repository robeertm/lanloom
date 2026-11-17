"""Client settings in the firewall editor (stage B): name and fixed address next to the move - preview, plan, gate,
draft and undo through the API with roles and the audit log, and the nightly report section "Changes"."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom.firewall import adapter, model
from lanloom.firewall.editor import Editor
from lanloom.main import create_app
from lanloom.nightly.contrib import REGISTRY, Ctx

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
ORIGIN = "http://lanloom.test"


def st():
    return adapter.sample_state()


# ---------------------------------------------------------------------------- preview and plan
def test_sample_devices_carry_their_fixed_address():
    devs = {d["id"]: d for d in st()["devices"]}
    assert devs["d19"]["fixed_ip"] == devs["d19"]["ip"] and devs["d27"]["fixed_ip"] == ""


def test_rename_and_fixed_address_give_one_client_step():
    s = st()
    pv = model.preview(s, {}, edits={"d19": {"name": "Washer", "fixed_ip": "192.168.60.90"}})
    assert not pv["blocks"]
    assert pv["steps"] == [{"kind": "client.set", "object": "d19", "before": {"fixed_ip": "192.168.60.60", "name": "Washer plug"},
                            "after": {"name": "Washer", "fixed_ip": "192.168.60.90"}}]
    ch = pv["changes"][0]
    assert ch["edit"] == [{"field": "fixed_ip", "from": "192.168.60.60", "to": "192.168.60.90"},
                          {"field": "name", "from": "Washer plug", "to": "Washer"}]
    assert ["fw.warn.fixed_ip_lease", {"name": "Washer plug", "ip": "192.168.60.90"}] in pv["warnings"]
    plan = model.build_plan(s, pv, target="simulation", user="admin", confirmed=[])
    assert plan["v"] == 2
    model.validate_plan(plan)


def test_moves_alone_stay_a_v1_plan():
    s = st()
    pv = model.preview(s, {"d19": "n_cams"})
    assert [x["kind"] for x in pv["steps"]] == ["move"]
    assert model.build_plan(s, pv, target="simulation", user="a", confirmed=[])["v"] == 1


def test_move_with_a_rename_is_one_step_with_the_network():
    s = st()
    pv = model.preview(s, {"d19": "n_cams", "d20": "n_cams"}, edits={"d19": {"name": "Washer"}})
    kinds = {x.get("object") or x.get("device"): x for x in pv["steps"]}
    assert kinds["d20"]["kind"] == "move"
    assert kinds["d19"]["kind"] == "client.set" and kinds["d19"]["after"] == {"network": "n_cams", "fixed_ip": "192.168.70.60", "name": "Washer"}
    assert kinds["d19"]["before"] == {"network": "n_smart", "fixed_ip": "192.168.60.60", "name": "Washer plug"}
    assert pv["changes"][0]["edit"] == [{"field": "name", "from": "Washer plug", "to": "Washer"}]


def test_a_chosen_fixed_address_wins_over_the_picked_one():
    pv = model.preview(st(), {"d19": "n_cams"}, edits={"d19": {"fixed_ip": "192.168.70.99"}})
    assert pv["steps"][0]["after"]["fixed_ip"] == "192.168.70.99" and pv["changes"][0]["new_ip"] == "192.168.70.99"


@pytest.mark.parametrize("ip,key", [("10.0.0.5", "fw.block.ip_outside"), ("192.168.60.0", "fw.block.ip_reserved"),
                                    ("192.168.60.1", "fw.block.ip_reserved"), ("192.168.60.255", "fw.block.ip_reserved"),
                                    ("192.168.60.61", "fw.block.ip_taken")])
def test_address_problems_block_the_preview(ip, key):
    pv = model.preview(st(), {}, edits={"d19": {"fixed_ip": ip}})
    assert pv["blocks"][0][0] == key and not pv["steps"]


def test_two_devices_cannot_get_the_same_new_address():
    pv = model.preview(st(), {}, edits={"d19": {"fixed_ip": "192.168.60.90"}, "d20": {"fixed_ip": "192.168.60.90"}})
    assert pv["blocks"] == [["fw.block.ip_taken", {"ip": "192.168.60.90", "name": "Hall thermostat"}]]


def test_protected_devices_are_not_edited():
    pv = model.preview(st(), {}, edits={"d01": {"name": "Router"}})
    assert pv["blocks"][0][0] == "fw.block.protected"


def test_removing_a_fixed_address_warns_about_users_by_address():
    pv = model.preview(st(), {}, edits={"d12": {"fixed_ip": ""}})
    keys = [w[0] for w in pv["warnings"]]
    assert "fw.warn.fixed_ip_removed" in keys and "fw.warn.dependency" in keys


def test_own_device_needs_a_confirmation_for_its_address():
    s = st()
    pv = model.preview(s, {}, edits={"d08": {"fixed_ip": "192.168.50.88"}}, requester_ip="192.168.50.20")
    assert "self" in pv["confirm"]
    pv = model.preview(s, {}, edits={"d08": {"name": "Laptop"}}, requester_ip="192.168.50.20")
    assert pv["confirm"] == []


def test_clean_edits_drops_noise():
    assert model.clean_edits(st(), {"d19": {"name": "Washer plug", "fixed_ip": "nope", "evil": "x"}, "zz": {"name": "a"},
                                    "d20": {"name": "  Thermostat  "}, "d21": "x"}) == {"d20": {"name": "Thermostat"}}


# ---------------------------------------------------------------------------- the gate (fresh read in the executor)
def _plan(s, edits, moves=None):
    pv = model.preview(s, moves or {}, edits=edits)
    return model.build_plan(s, pv, target="simulation", user="a", confirmed=pv["confirm"])


def _gate(s, plan, caps=None):
    from lanloom.integrations.simulation import CAPABILITIES
    model.gate(s, plan, target="simulation", now=plan["created"], quiet=False, busy=False, healthy=(True, ""), caps=caps or CAPABILITIES)


def test_gate_accepts_a_fresh_plan_and_refuses_changed_clients():
    s = st()
    plan = _plan(s, {"d19": {"name": "Washer"}})
    _gate(s, plan)
    s2 = st()
    next(d for d in s2["devices"] if d["id"] == "d19")["name"] = "Changed meanwhile"
    with pytest.raises(model.Refused) as exc:
        _gate(s2, plan)
    assert exc.value.key in ("fw.block.conflict",)


def test_gate_checks_addresses_itself():
    s = st()
    plan = _plan(s, {"d19": {"fixed_ip": "192.168.60.90"}})
    plan["steps"][0]["after"]["fixed_ip"] = "10.1.1.1"
    with pytest.raises(model.Refused) as exc:
        _gate(s, plan)
    assert exc.value.key == "fw.block.ip_outside"
    plan["steps"][0]["after"]["fixed_ip"] = "192.168.60.61"
    with pytest.raises(model.Refused) as exc:
        _gate(s, plan)
    assert exc.value.key == "fw.block.ip_taken"


def test_gate_refuses_duplicate_addresses_across_steps():
    s = st()
    plan = _plan(s, {"d19": {"fixed_ip": "192.168.60.90"}, "d20": {"fixed_ip": "192.168.60.91"}})
    plan["steps"][1]["after"]["fixed_ip"] = "192.168.60.90"
    with pytest.raises(model.Refused) as exc:
        _gate(s, plan)
    assert exc.value.key == "fw.block.conflict"


def test_gate_refuses_client_steps_an_adapter_cannot_do():
    from lanloom.integrations.capabilities import V1_DEFAULT
    s = st()
    plan = _plan(s, {"d19": {"name": "Washer"}})
    with pytest.raises(model.Refused) as exc:
        _gate(s, plan, caps=V1_DEFAULT)
    assert exc.value.key == "fw.block.capability"


def test_simulation_applies_and_restores_client_settings(tmp_path):
    from lanloom.firewall.runner import Runner
    sim = adapter.SimulatedController(settle_s=0, fail_after_writes=2)
    s = sim.read()
    plan = _plan(s, {"d19": {"name": "Washer"}, "d20": {"fixed_ip": "192.168.60.91"}})
    r = Runner(sim, tmp_path, target="simulation", require_signature=False, watch_s=0, check_s=0, sleep=lambda x: None)
    res = r.run(plan)
    assert res["status"] == "rolled_back"
    devs = {d["id"]: d for d in sim.read()["devices"]}
    assert devs["d19"]["name"] == "Washer plug" and devs["d20"]["fixed_ip"] == "192.168.60.61"
    sim.fail_after_writes = 0
    plan = _plan(sim.read(), {"d19": {"name": "Washer"}}, {"d20": "n_cams"})
    res = Runner(sim, tmp_path / "2", target="simulation", require_signature=False, watch_s=0, check_s=0, sleep=lambda x: None).run(plan)
    assert res["status"] == "done"
    devs = {d["id"]: d for d in sim.read()["devices"]}
    assert devs["d19"]["name"] == "Washer" and devs["d20"]["network"] == "n_cams" and devs["d20"]["fixed_ip"] == devs["d20"]["ip"]


# ---------------------------------------------------------------------------- through the API
@pytest.fixture()
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.setenv("LANLOOM_FW_SIM_WATCH_S", "0")
    monkeypatch.setenv("LANLOOM_FW_SIM_SETTLE_S", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url=ORIGIN) as client:
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        assert client.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200
        client.ll = app.state.ll
        yield client


def wait_done(c):
    for _ in range(600):              # up to 30 s: the simulation runs in a thread, slow under a loaded test machine
        v = c.get("/api/firewall").json()
        if not v["running"]:
            return v
        time.sleep(0.05)
    raise AssertionError("simulation did not finish")


def test_draft_preview_apply_and_undo_of_client_settings(c):
    v = c.get("/api/firewall").json()
    assert {"client.set.name", "client.set.fixed_ip"} <= set(v["capabilities"]["write"])
    d = c.put("/api/firewall/draft", json={"moves": {}, "edits": {"d19": {"name": "Washer", "fixed_ip": "192.168.60.90"}}}, headers=HDR).json()
    assert d["edits"] == {"d19": {"name": "Washer", "fixed_ip": "192.168.60.90"}}
    # a client of an earlier release sends moves only: the edits stay
    d = c.put("/api/firewall/draft", json={"moves": {"d20": "n_cams"}}, headers=HDR).json()
    assert d["moves"] == {"d20": "n_cams"} and d["edits"]["d19"]["name"] == "Washer"
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert sorted(s["kind"] for s in pv["steps"]) == ["client.set", "move"]
    p = c.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR).json()
    assert p["plan"]["v"] == 2
    assert c.post("/api/firewall/approve", json={"plan": p["plan"]["id"]}, headers=HDR).json()["ok"]
    v = wait_done(c)
    job = v["history"][0]
    assert job["status"] == "done" and job["target"] == "simulation"
    named = next(x for x in job["summary"] if x.get("kind") == "client.set")
    assert named["name_new"] == "Washer" and named["fixed_ip"] == "192.168.60.90"
    devs = {x["id"]: x for x in v["state"]["devices"]}
    assert devs["d19"]["name"] == "Washer" and devs["d19"]["fixed_ip"] == "192.168.60.90"
    assert v["draft"] == {"moves": {}, "edits": {}, "t": 0, "undo_of": None}
    d = c.post("/api/firewall/undo", json={"plan": job["plan"]}, headers=HDR).json()
    assert d["edits"] == {"d19": {"name": "Washer plug", "fixed_ip": "192.168.60.60"}} and d["moves"] == {"d20": "n_smart"}
    entries = c.get("/api/audit?limit=20").json()["entries"]
    drafts = [e for e in entries if e["action"] == "firewall.draft"]
    assert drafts and drafts[-1]["detail"] == {"moves": 0, "edits": 1}


def test_draft_refuses_too_many_devices(c):
    edits = {"d%02d" % i: {"name": "Device %d" % i} for i in range(8, 20)}
    r = c.put("/api/firewall/draft", json={"moves": {}, "edits": edits}, headers=HDR)
    assert r.status_code == 409 and r.json()["error"] == "fw.block.too_many"


def test_viewer_cannot_draft_and_operator_cannot_approve(c):
    c.ll.accounts.create("vic", PW, role="viewer")
    c.ll.accounts.create("op", PW, role="operator")
    c.cookies.clear()
    assert c.post("/api/login", json={"username": "vic", "password": PW}, headers=HDR).status_code == 200
    r = c.put("/api/firewall/draft", json={"edits": {"d19": {"name": "x"}}}, headers=HDR)
    assert r.status_code == 403
    c.cookies.clear()
    assert c.post("/api/login", json={"username": "op", "password": PW}, headers=HDR).status_code == 200
    assert c.put("/api/firewall/draft", json={"edits": {"d19": {"name": "Washer"}}}, headers=HDR).status_code == 200
    assert c.post("/api/firewall/preview", json={}, headers=HDR).status_code == 200
    assert c.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR).status_code == 403


def test_live_sources_never_offer_writes(c):
    ed = c.ll.firewall
    ed.live = lambda: [{"key": "int:9", "name": "Hall console", "family": "fam-a", "age_s": 1, "state": st(),
                        "caps": {"family": "fam-a", "write": {"move": "B", "client.set.name": "B"}, "read": []}}]
    v = c.get("/api/firewall?source=int:9").json()
    assert v["mode"] == "live" and v["capabilities"]["write"] == {}


# ---------------------------------------------------------------------------- nightly report section "Changes"
class _Journal:
    def __init__(self, events):
        self.events = events

    def add(self, kind, data=None, t=None):
        self.events.append({"kind": kind, "data": data or {}, "t": t or time.time()})

    def between(self, since, until, kinds=None):
        return [e for e in self.events if since < e["t"] <= until and (not kinds or e["kind"] in kinds)]

    def any(self, kinds):
        return any(e["kind"] in kinds for e in self.events)


def _section(events, monkeypatch, external=False):
    from lanloom.firewall import report  # noqa: F401 - registers the section
    if external:
        monkeypatch.setenv("LANLOOM_FW_EXECUTOR", "external")
    else:
        monkeypatch.delenv("LANLOOM_FW_EXECUTOR", raising=False)
    contrib = next(x for x in REGISTRY if x.id == "changes")
    ctx = Ctx(core=None, journal=_Journal(events), since=1_800_000_000, until=1_800_086_400, prev=None)
    return contrib.build(ctx) if contrib.available(ctx) else None


T0 = 1_800_040_000      # a fixed clock for the report data


def test_changes_section_only_with_a_source(monkeypatch):
    assert _section([], monkeypatch) is None
    sec = _section([], monkeypatch, external=True)
    assert sec["summary"]["key"] == "report.changes.calm" and sec["items"] == []


def test_changes_section_lists_results(monkeypatch):
    ev = [{"kind": "fw_change", "t": T0, "data": {"plan": "p1", "user": "admin", "status": "done", "n": 2, "names": ["Phone", "Printer"]}},
          {"kind": "fw_change", "t": T0 + 60, "data": {"plan": "p2", "user": "admin", "status": "rolled_back", "reason": "fw.res.rolled_back_device",
                                                       "n": 1, "names": ["Tablet"]}},
          {"kind": "fw_change", "t": T0 + 120, "data": {"plan": "p3", "user": "op", "status": "refused", "reason": "fw.block.conflict", "n": 1,
                                                        "names": ["TV"]}}]
    sec = _section(ev, monkeypatch)
    assert sec["id"] == "changes" and sec["level"] == "warn" and sec["summary"]["key"] == "report.changes.summary_trouble"
    keys = [i["key"] for i in sec["items"]]
    assert keys[0] == "report.changes.rolled_back" and set(keys) == {"report.changes.done", "report.changes.rolled_back", "report.changes.refused"}
    done = next(i for i in sec["items"] if i["key"] == "report.changes.done")
    assert done["vars"]["what"] == "Phone, Printer" and done["vars"]["at_at"] == T0 and not done["point"]
    rb = sec["items"][0]
    assert rb["point"] and rb["vars"]["reason_key"] == "fw.res.rolled_back_device"


@pytest.mark.parametrize("lang", ["en", "de"])
def test_changes_section_renders_in_every_language(monkeypatch, lang):
    from datetime import timezone
    from lanloom.nightly.render import Renderer, strings
    ev = [{"kind": "fw_change", "t": T0, "data": {"plan": "p1", "user": "admin", "status": "done", "n": 4,
                                                  "names": ["Phone", "Printer", "TV", "Tablet"]}},
          {"kind": "fw_change", "t": T0 + 5, "data": {"plan": "p2", "user": "admin", "status": "failed", "reason": "fw.res.rollback_failed",
                                                      "n": 1, "names": ["Speaker"]}}]
    sec = _section(ev, monkeypatch)
    r = Renderer(lang, timezone.utc, "2027-01-15")
    tr = strings(lang)
    assert tr[sec["title"]] and r.tr(sec["summary"]["key"], **r.values(sec["summary"]["vars"]))
    for it in sec["items"]:
        text = r.line(it)
        assert "{" not in text and "Phone, Printer, TV +1" in text or "Speaker" in text


def test_editor_journals_real_changes_but_not_rehearsals(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))

    class Store:
        def __init__(self):
            self.s = {}

        def get_setting(self, k, d=None):
            return self.s.get(k, d)

        def set_setting(self, k, v):
            self.s[k] = v

    ed = Editor(config_mod.load(), Store())
    ed.journal = _Journal([])
    for target, pid in (("simulation", "p_sim"), ("controller", "p_real")):
        ed.store.set_setting("fw_jobs", [{"plan": pid, "user": "admin", "status": "running", "summary": [{"name": "Phone"}], "target": target}]
                             + (ed.history() if pid == "p_real" else []))
        ed.on_progress({"plan": pid, "stage": "done", "status": "done", "reason": "fw.res.done"})
    ed.on_progress({"plan": "p_real", "stage": "done", "status": "done"})          # a repeated report is journaled once
    assert [e["data"]["plan"] for e in ed.journal.events] == ["p_real"]
    assert ed.journal.events[0]["data"]["names"] == ["Phone"] and ed.journal.events[0]["kind"] == "fw_change"


# ---------------------------------------------------------------------------- devices the controller cannot move by a client setting
def test_device_whose_network_follows_its_port_is_not_a_drop_target():
    s = st()
    dev = next(d for d in s["devices"] if d["id"] == "d07")
    dev["network_by_port"] = True
    assert set(model.drop_targets(s, dev).values()) <= {"fw.why.wired_port", "fw.why.same", "fw.why.management"}
    pv = model.preview(s, {"d07": "n_work"})
    assert pv["blocks"] == [["fw.block.wired_move", {"name": "Backup server", "reason": ""}]] and not pv["steps"]
    pv = model.preview(s, {}, edits={"d07": {"name": "Backups"}})           # renaming still works
    assert not pv["blocks"] and pv["steps"][0]["kind"] == "client.set"


def test_device_without_a_known_network_can_be_renamed_only():
    s = st()
    dev = next(d for d in s["devices"] if d["id"] == "d19")
    dev["network"] = None
    pv = model.preview(s, {"d19": "n_cams"})
    assert pv["blocks"] == [["fw.block.no_network", {"name": "Washer plug"}]]
    pv = model.preview(s, {}, edits={"d19": {"fixed_ip": "192.168.60.90"}})
    assert pv["blocks"] == [["fw.block.no_network", {"name": "Washer plug"}]]
    pv = model.preview(s, {}, edits={"d19": {"name": "Washer"}})
    assert not pv["blocks"]



def test_a_change_put_back_is_a_notification_event(tmp_path, monkeypatch):
    from lanloom.firewall import report  # noqa: F401 - registers the event
    from lanloom.notify import events
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    ev = events.get("fw_change")
    assert ev and ev.group == "changes" and ev.title == "notify.ev.fw_change"

    class Store:
        def __init__(self):
            self.s = {}

        def get_setting(self, k, d=None):
            return self.s.get(k, d)

        def set_setting(self, k, v):
            self.s[k] = v

    class Notify:
        def __init__(self):
            self.sent = []

        def emit(self, eid, key, **kw):
            self.sent.append((eid, key, kw))

    ed = Editor(config_mod.load(), Store())
    ed.notify = Notify()
    for pid, status, target in (("p1", "done", "controller"), ("p2", "rolled_back", "controller"), ("p3", "failed", "controller"),
                                ("p4", "rolled_back", "simulation")):
        ed.store.set_setting("fw_jobs", [{"plan": pid, "user": "admin", "status": "running", "summary": [{"name": "Phone"}], "target": target}])
        ed.on_progress({"plan": pid, "stage": "done", "status": status, "reason": "fw.res.rolled_back_device"})
    assert [(e[1], e[2]["severity"]) for e in ed.notify.sent] == [("fw_change:p2", "warning"), ("fw_change:p3", "critical")]
    assert ed.notify.sent[0][2]["data"] == {"what": "Phone", "who": "admin", "result_key": "fw.res.rolled_back_device"}
