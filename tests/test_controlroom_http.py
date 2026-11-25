"""HTTP routes of the control room (lanloom/web_controlroom.py) against the sample network: rights per role, admin data, audit lines,
the chain life cycle, and the reason keys the backend can produce all have texts."""
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import access, auth
from lanloom import config as config_mod
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}
ROOT = Path(__file__).resolve().parent.parent

READS = ["/api/controlroom", "/api/wan", "/api/nas", "/api/ups", "/api/backups"]
CHANGES = [("PUT", "/api/wan/probe", {"tcp": ["192.0.2.53:53"]}),
           ("PUT", "/api/backups/chains/rt-chain", {"name": "RT", "expect_hours": 24}),
           ("DELETE", "/api/backups/chains/rt-chain", None),
           ("POST", "/api/backups/chains/rt-chain/run", {"ok": True}),
           ("POST", "/api/backups/chains/rt-chain/progress", {"pct": 10}),
           ("POST", "/api/backups/restore-check", {"chain": "manual:rt-chain"}),
           ("POST", "/api/backups/restore-check/undo", {"chain": "manual:rt-chain"}),
           ("POST", "/api/backups/ignore", {"chain": "hub:rt"})]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("crhttp")
    mp = pytest.MonkeyPatch()
    mp.setenv("LANLOOM_DATA", str(tmp))
    mp.setenv("LANLOOM_DEMO", "1")
    mp.setenv("LANLOOM_DEMO_SEED", "1")
    mp.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        c.st = st
        c.uids = {role: st.accounts.create("user-" + role, "a-long-test-password", role=role) for role in access.ROLES}
        deadline = time.time() + 30
        while time.time() < deadline:                       # the sample network seeds its control room a moment after the start
            c.cookies.clear()
            c.cookies.set(auth.SESSION_COOKIE, st.sessions.create(c.uids["admin"], ip="127.0.0.1", agent="test"))
            ids = [b["id"] for b in c.get("/api/controlroom").json().get("blocks", [])]
            if {"gateway", "wan", "nas", "ups", "backups"} <= set(ids):
                break
            time.sleep(0.3)
        c.cookies.clear()
        yield c
    mp.undo()


def login(c, role):
    c.cookies.clear()
    c.cookies.set(auth.SESSION_COOKIE, c.st.sessions.create(c.uids[role], ip="127.0.0.1", agent="test"))


def call(c, method, path, body=None, role=None):
    if role:
        login(c, role)
    else:
        c.cookies.clear()
    try:
        return c.request(method, path, headers=HDR, json=body if body is not None else {})
    finally:
        c.cookies.clear()


# ---------------------------------------------------------------------------------------------------- who may do what
def test_the_rights_table_names_every_route():
    want = {("GET", p) for p in READS} | {(m, p.replace("rt-chain", "{chain_id}")) for m, p, _ in CHANGES if "rt-chain" in p or p == "/api/backups/ignore"}
    want |= {("PUT", "/api/wan/probe"), ("POST", "/api/backups/restore-check"), ("GET", "/api/ups")}
    for key in want:
        assert key in access.RULES, key
    assert access.RULES[("PUT", "/api/wan/probe")].role == "admin"
    for key in (("PUT", "/api/backups/chains/{chain_id}"), ("DELETE", "/api/backups/chains/{chain_id}"), ("POST", "/api/backups/chains/{chain_id}/run"),
                ("POST", "/api/backups/chains/{chain_id}/progress"), ("POST", "/api/backups/restore-check"),
                ("POST", "/api/backups/restore-check/undo"), ("POST", "/api/backups/ignore")):
        assert access.RULES[key].role == "operator" and access.RULES[key].audit, key
    assert access.RULES[("PUT", "/api/wan/probe")].audit == "wan.probe"
    assert all(access.RULES[("GET", p)].role == "viewer" for p in READS)


@pytest.mark.parametrize("path", READS)
def test_reads_need_a_sign_in(client, path):
    assert call(client, "GET", path).status_code == 401


@pytest.mark.parametrize("method,path,body", CHANGES)
def test_changes_need_a_sign_in(client, method, path, body):
    assert call(client, method, path, body).status_code == 401


@pytest.mark.parametrize("role", ["viewer", "operator", "admin"])
@pytest.mark.parametrize("path", READS)
def test_everybody_signed_in_reads(client, path, role):
    r = call(client, "GET", path, role=role)
    assert r.status_code == 200 and isinstance(r.json(), dict)


@pytest.mark.parametrize("method,path,body", CHANGES)
def test_a_viewer_cannot_change_anything(client, method, path, body):
    r = call(client, method, path, body, role="viewer")
    assert r.status_code == 403 and r.json()["error"] == "auth.err.role"


def test_only_an_admin_picks_the_probe_targets(client):
    r = call(client, "PUT", "/api/wan/probe", {"tcp": ["192.0.2.53:53"]}, role="operator")
    assert r.status_code == 403 and r.json()["error"] == "auth.err.role"
    r = call(client, "PUT", "/api/wan/probe", {"tcp": ["192.0.2.53:53"], "dns": "Example.org"}, role="admin")
    assert r.status_code == 200 and r.json() == {"enabled": True, "dns": "example.org", "tcp": ["192.0.2.53:53"]}
    assert call(client, "GET", "/api/wan", role="viewer").json()["probe"]["tcp"] == ["192.0.2.53:53"]


def test_probe_targets_are_validated(client):
    for body, err in (({"tcp": ["bad target"]}, "wan.err.probe_target"), ({"dns": "-x"}, "wan.err.probe_name"),
                      ({"tcp": ["192.0.2.%d:53" % i for i in range(1, 6)]}, "wan.err.probe_many"), ({"tcp": "192.0.2.1:53"}, "wan.err.probe_target")):
        r = call(client, "PUT", "/api/wan/probe", body, role="admin")
        assert r.status_code == 400 and r.json()["error"] == err, body
    r = call(client, "PUT", "/api/wan/probe", {}, role="admin")
    assert r.status_code == 200 and r.json()["enabled"] is False                         # no target: the check is off
    login(client, "admin")
    assert client.put("/api/wan/probe", content="not json", headers=HDR).status_code == 400
    client.cookies.clear()


# ---------------------------------------------------------------------------------------------------- the chain life cycle
def test_a_chain_from_declaration_to_deletion_as_operator(client):
    base = "/api/backups/chains/rt-life"
    r = call(client, "PUT", base, {"name": "Life", "source": "A", "target": "B", "expect_hours": 24}, role="operator")
    assert r.status_code == 200 and r.json() == {"id": "rt-life"}

    def chain():
        return next(c for c in call(client, "GET", "/api/backups", role="viewer").json()["chains"] if c["id"] == "manual:rt-life")
    assert chain()["status"] == "unknown" and chain()["editable"] is True
    assert call(client, "POST", base + "/progress", {"pct": 40}, role="operator").json() == {"ok": True}
    assert chain()["status"] == "running" and chain()["progress"]["pct"] == 40.0
    assert call(client, "POST", base + "/run", {"ok": True, "size": 5 * 10 ** 9, "duration_s": 600}, role="operator").json() == {"ok": True}
    c = chain()
    assert c["status"] == "ok" and c["progress"] is None and c["restore_hint"] == "never"
    assert call(client, "POST", "/api/backups/restore-check", {"chain": "manual:rt-life"}, role="operator").json() == {"ok": True}
    assert chain()["restore_hint"] == "ok"
    assert call(client, "POST", base + "/run", {"ok": False}, role="operator").status_code == 200
    assert chain()["status"] == "failed"
    assert call(client, "DELETE", base, role="operator").json() == {"ok": True}
    assert call(client, "DELETE", base, role="operator").status_code == 404
    assert not any(c["id"] == "manual:rt-life" for c in call(client, "GET", "/api/backups", role="viewer").json()["chains"])


def test_a_restore_date_is_taken_back_over_http(client):
    base = "/api/backups/chains/rt-undo"
    call(client, "PUT", base, {"name": "Undo", "expect_hours": 24}, role="operator")

    def chain():
        return next(c for c in call(client, "GET", "/api/backups", role="viewer").json()["chains"] if c["id"] == "manual:rt-undo")
    undo = "/api/backups/restore-check/undo"
    assert call(client, "POST", undo, {"chain": "manual:rt-undo"}, role="operator").status_code == 404      # nothing noted yet
    call(client, "POST", "/api/backups/restore-check", {"chain": "manual:rt-undo"}, role="operator")
    assert chain()["restore_hint"] == "ok"
    assert call(client, "POST", undo, {"chain": "manual:rt-undo"}, role="viewer").status_code == 403
    assert call(client, "POST", undo, {"chain": "manual:rt-undo"}, role="operator").json() == {"ok": True}
    assert chain()["restore_hint"] == "never" and chain()["restore_checked_t"] is None
    call(client, "DELETE", base, role="operator")


def test_an_automatic_chain_is_set_to_not_used_and_back_over_http(client):
    st = client.st
    ents = [{"entity_id": "sensor.backup_manager_state", "device_id": "d1", "name": "Backup Manager state", "unit": "", "dc": None, "domain": "sensor"}]
    now = time.time()
    st.core.hub = {1: {"snapshot": {"t": now, "devices": [{"id": "d1", "name": "Hub"}], "entities": ents},
                       "states": {"sensor.backup_manager_state": ("idle", now)}}}
    ig = "/api/backups/ignore"

    def view():
        return call(client, "GET", "/api/backups", role="viewer").json()
    try:
        assert [c["id"] for c in view()["chains"] if c["kind"] == "hub"] == ["hub:1"]

        def none_count():
            pts = call(client, "GET", "/api/controlroom", role="viewer").json()["points"]
            return sum(p["vars"].get("n", 0) for p in pts if p["block"] == "backups" and p["key"] == "cr.bk.none")
        before = none_count()                                               # the hub reports nothing yet: "no backup at all", plus the sample's own
        assert before >= 1
        assert call(client, "POST", ig, {"chain": "hub:1", "ignored": True}, role="viewer").status_code == 403
        assert call(client, "POST", ig, {"chain": "hub:1", "ignored": True}, role="operator").json() == {"ok": True, "ignored": True}
        v = view()
        assert not any(c["kind"] == "hub" for c in v["chains"]) and [c["id"] for c in v["ignored"]] == ["hub:1"]
        assert none_count() == before - 1
        assert call(client, "POST", ig, {"chain": "hub:nope", "ignored": True}, role="operator").status_code == 404
        r = call(client, "POST", ig, {"chain": "manual:x", "ignored": True}, role="operator")
        assert r.status_code == 400 and r.json()["error"] == "chain.err.declared"
        assert call(client, "POST", ig, {"chain": "hub:1", "ignored": False}, role="operator").json() == {"ok": True, "ignored": False}
        assert [c["id"] for c in view()["chains"] if c["kind"] == "hub"] == ["hub:1"] and view()["ignored"] == []
        done = [(a, t, d) for a, o, _w, t, d in audit_actions(client) if o == "ok" and a == "chain.ignore"]
        assert ("chain.ignore", "hub:1", {"ignored": True}) in done and ("chain.ignore", "hub:1", {"ignored": False}) in done
    finally:
        call(client, "POST", ig, {"chain": "hub:1", "ignored": False}, role="operator")
        st.core.hub = {}


def test_a_script_reports_a_run_over_the_public_api(client):
    assert call(client, "PUT", "/api/backups/chains/rt-v1", {"name": "V1", "expect_hours": 24}, role="operator").status_code == 200
    assert call(client, "POST", "/api/v1/backups/rt-v1/run", {"ok": True, "size": 10 ** 9, "duration_s": 100}, role="viewer").status_code == 403
    assert call(client, "POST", "/api/v1/backups/rt-v1/run", {"ok": True, "size": 10 ** 9, "duration_s": 100}, role="operator").json() == {"ok": True}
    assert call(client, "POST", "/api/v1/backups/nobody/run", {"ok": True}, role="operator").status_code == 404
    rows = call(client, "GET", "/api/v1/backups", role="viewer").json()["chains"]
    mine = next(c for c in rows if c["id"] == "rt-v1")
    assert mine["status"] == "ok" and mine["kind"] == "manual"
    assert call(client, "DELETE", "/api/backups/chains/rt-v1", role="operator").json() == {"ok": True}


def test_chain_input_errors(client):
    for chain_id, body, err in (("Bad%20Id", {"name": "x", "expect_hours": 24}, "chain.err.id"), ("rt-err", {"expect_hours": 24}, "chain.err.name"),
                                ("rt-err", {"name": "x", "expect_hours": 0}, "chain.err.expect"), ("rt-err", {"name": "x"}, "chain.err.expect")):
        r = call(client, "PUT", "/api/backups/chains/" + chain_id, body, role="operator")
        assert r.status_code == 400 and r.json()["error"] == err, (chain_id, body)
    for method, path, body in (("DELETE", "/api/backups/chains/nope", None), ("POST", "/api/backups/chains/nope/run", {"ok": True}),
                               ("POST", "/api/backups/chains/nope/progress", {"pct": 1}), ("POST", "/api/backups/restore-check", {"chain": "manual:nope"}),
                               ("POST", "/api/backups/restore-check", {})):
        r = call(client, method, path, body, role="operator")
        assert r.status_code == 404 and r.json()["error"] == "chain.err.missing", (method, path)


def test_a_chain_names_its_devices_over_http(client):
    body = {"name": "C", "expect_hours": 24, "devices": ["d-1"]}
    assert call(client, "PUT", "/api/backups/chains/rt-cover", body, role="operator").status_code == 200
    try:
        rows = call(client, "GET", "/api/backups", role="viewer").json()["chains"]
        assert next(c for c in rows if c["id"] == "manual:rt-cover")["devices"] == ["d-1"]
        r = call(client, "PUT", "/api/backups/chains/rt-cover", dict(body, devices=["d-%d" % i for i in range(21)]), role="operator")
        assert r.status_code == 400 and r.json()["error"] == "chain.err.devices"
    finally:
        call(client, "DELETE", "/api/backups/chains/rt-cover", role="operator")


def test_restore_check_dates(client):
    call(client, "PUT", "/api/backups/chains/rt-date", {"name": "D", "expect_hours": 24}, role="operator")
    for t in (time.time() + 86400, "yesterday", 5.0):
        r = call(client, "POST", "/api/backups/restore-check", {"chain": "manual:rt-date", "t": t}, role="operator")
        assert r.status_code == 400 and r.json()["error"] == "chain.err.date", t
    ok = call(client, "POST", "/api/backups/restore-check", {"chain": "manual:rt-date", "t": time.time() - 86400}, role="operator")
    assert ok.status_code == 200
    call(client, "DELETE", "/api/backups/chains/rt-date", role="operator")


def test_a_hypervisor_chain_cannot_be_edited_by_id(client):
    chains = call(client, "GET", "/api/backups", role="viewer").json()["chains"]
    hv = next((c for c in chains if c["kind"] != "manual"), None)
    assert hv is not None and hv["editable"] is False
    r = call(client, "DELETE", "/api/backups/chains/" + hv["id"], role="operator")
    assert r.status_code == 404


# ---------------------------------------------------------------------------------------------------- admin data
def cell_cards(j):
    return j["cellular"] if "cellular" in j else j["wan"]["cellular"]


@pytest.mark.parametrize("path", ["/api/wan", "/api/controlroom"])
def test_cell_id_tac_and_position_are_for_admins_only(client, path):
    for role in ("viewer", "operator"):
        r = call(client, "GET", path, role=role)
        cards = cell_cards(r.json())
        assert cards and all("admin" not in c for c in cards)
        for secret in ("0x1A2B3C4", "4711", "Sample hill"):
            assert secret not in r.text, (role, secret)
    adm = cell_cards(call(client, "GET", path, role="admin").json())
    assert adm[0]["admin"] == {"cell_id": "0x1A2B3C4", "tac": "4711", "location": "Sample hill"}


def test_the_storage_boxes_never_carry_admin_data(client):
    for role in ("viewer", "admin"):
        r = call(client, "GET", "/api/nas", role=role)
        assert "cards" in r.json() and "0x1A2B3C4" not in r.text


# ---------------------------------------------------------------------------------------------------- what the sample network shows
def test_the_sample_network_has_all_blocks(client):
    snap = call(client, "GET", "/api/controlroom", role="viewer").json()
    ids = [b["id"] for b in snap["blocks"]]
    assert {"gateway", "wan", "nas", "ups", "backups"} <= set(ids)
    for b in snap["blocks"]:
        assert set(b) == {"id", "level", "fresh", "age_s", "lines", "link"} and b["level"] in ("ok", "warn", "danger", "unknown")
        assert b["lines"] and all({"key", "vars", "level"} == set(ln) and ln["key"].startswith("cr.") for ln in b["lines"])
    assert snap["level"] in ("ok", "warn", "danger", "unknown") and snap["points"] and snap["sources"]
    assert all(p["level"] in ("warn", "danger") for p in snap["points"])


def test_the_sample_internet_watch(client):
    w = call(client, "GET", "/api/wan", role="viewer").json()
    assert w["state"] in ("up", "flapping") and len(w["lines"]) >= 2 and w["failover"]["has_reserve"] is True
    assert {"primary", "backup"} <= {ln["role"] for ln in w["lines"]}
    assert len(w["history"]["bar"]) == 96 and w["history"]["uptime_pct_7d"] is not None
    assert w["axes"]["gateway"]["from"] == "wans"
    assert [c["tech"] for c in w["cellular"]] == ["5G NSA"] and [b["band"] for b in w["cellular"][0]["bands"]] == ["B20", "B1", "B3", "n78"]
    assert {p["id"] for p in w["probe"]["presets"]} == {"resolvers", "lookup"}


def test_the_sample_ups_nas_and_backups(client):
    u = call(client, "GET", "/api/ups", role="viewer").json()
    assert u["units"][0]["ups"] == "office" and u["units"][0]["state"] == "online" and u["units"][0]["runtime_if_failed"] is True
    n = call(client, "GET", "/api/nas", role="viewer").json()
    assert n["cards"] and n["cards"][0]["volumes"] and n["cards"][0]["ring"]["pct"] is not None
    b = call(client, "GET", "/api/backups", role="viewer").json()
    assert b["restore_old_days"] == 180 and sum(b["summary"].values()) == len(b["chains"]) > 3
    statuses = {c["status"] for c in b["chains"]}
    assert {"ok", "late", "running"} <= statuses


# ---------------------------------------------------------------------------------------------------- audit
def audit_actions(c):
    return [(e["action"], e["outcome"], e["actor"], e["target"], e["detail"]) for e in c.st.audit.query({}, 200)]


def test_changes_and_refusals_end_up_in_the_audit_log(client):
    call(client, "PUT", "/api/backups/chains/rt-audit", {"name": "A", "expect_hours": 24}, role="operator")
    call(client, "POST", "/api/backups/chains/rt-audit/progress", {"pct": 5}, role="operator")
    call(client, "POST", "/api/backups/chains/rt-audit/run", {"ok": True}, role="operator")
    call(client, "POST", "/api/backups/restore-check", {"chain": "manual:rt-audit"}, role="operator")
    call(client, "DELETE", "/api/backups/chains/rt-audit", role="operator")
    call(client, "PUT", "/api/wan/probe", {"tcp": ["192.0.2.54:53"]}, role="admin")
    call(client, "PUT", "/api/wan/probe", {"tcp": ["192.0.2.55:53"]}, role="operator")
    rows = audit_actions(client)
    done = {(a, t) for a, o, who, t, _d in rows if o == "ok"}
    for action in ("chain.save", "chain.progress", "chain.report", "chain.delete"):
        assert (action, "rt-audit") in done, action
    assert ("chain.restore_check", "manual:rt-audit") in done
    probe = next(r for r in rows if r[0] == "wan.probe" and r[1] == "ok")
    assert probe[2] == "user-admin" and probe[4] == {"enabled": True, "targets": 1}
    assert any(a == "wan.probe" and o == "denied" and who == "user-operator" for a, o, who, _t, _d in rows)
    assert client.st.audit.verify()["ok"]


def test_a_read_leaves_no_audit_line(client):
    before = len(client.st.audit.query({}, 200))
    call(client, "GET", "/api/controlroom", role="viewer")
    call(client, "GET", "/api/wan", role="viewer")
    assert len(client.st.audit.query({}, 200)) == before


# ---------------------------------------------------------------------------------------------------- texts
def strings(lang):
    return json.loads((ROOT / "lanloom" / "i18n" / ("%s.json" % lang)).read_text(encoding="utf-8"))


def backend_keys():
    from lanloom.controlroom import cellular
    from lanloom.sources import REASON_KEYS
    keys = set()
    for name in ("room", "ups", "nas", "backups", "wan", "cellular", "report", "levels"):
        src = (ROOT / "lanloom" / "controlroom" / (name + ".py")).read_text(encoding="utf-8")
        keys |= set(re.findall(r'"((?:cr|wan|chain)\.[a-z_]+(?:\.[a-z_]+)*)"', src))
    keys |= set(re.findall(r'"((?:chain|wan)\.err\.[a-z_]+)"', (ROOT / "lanloom" / "web_controlroom.py").read_text(encoding="utf-8")))
    keys |= {"cr.wan.state." + s for s in ("up", "down", "flapping", "unknown")}
    keys |= {"cr.wan.disagree.gateway_down_measured_up", "cr.wan.disagree.gateway_up_measured_down"}
    keys |= {"cr.ups." + s for s in ("online", "battery", "low_battery", "replace", "overload", "unknown")}
    keys |= {"cr.grade." + g for g in cellular.GRADES + ("none",)}
    keys |= {"cr.report.backup_%s%s" % (s, x) for s in ("failed", "late", "unknown", "none") for x in ("", "_since")} | {"cr.report.wan_failover", "cr.report.wan_failback"}
    keys |= {k for k in REASON_KEYS if k.startswith("source.nut.")}
    keys |= {"audit.action." + r.audit for (m, p), r in access.RULES.items() if r.audit and (p.startswith("/api/backups") or p == "/api/wan/probe")}
    return {k for k in keys if not k.endswith((".", "_"))}                       # prefixes the code completes at run time are listed above


def test_every_key_the_backend_can_produce_has_an_english_and_a_german_text():
    en, de = strings("en"), strings("de")
    keys = backend_keys()
    assert len(keys) > 80
    assert [k for k in sorted(keys) if k not in en] == []
    assert [k for k in sorted(keys) if k not in de] == []
    for k in keys:
        assert en[k].strip() and de[k].strip(), k


def test_the_texts_of_the_control_room_use_matching_placeholders_in_both_languages():
    en, de = strings("en"), strings("de")
    for k in backend_keys():
        a = set(re.findall(r"\{(\w+)\s*[,}]", en[k]))
        b = set(re.findall(r"\{(\w+)\s*[,}]", de[k]))
        assert a == b, (k, a, b)
