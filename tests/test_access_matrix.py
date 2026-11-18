"""Every route of the app against the one rights table (lanloom/access.py).

* The table names exactly the routes the app has.
* For every route that needs a session: a role below the table's role gets 403 auth.err.role, the table's role and
  higher never do. A route with step-up refuses a session whose last confirmation is old.
* Every state-changing route names its audit action, and calls really end up in the audit log.
"""
import re

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient

from lanloom import access, auth
from lanloom import config as config_mod
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}
CHANGING = ("POST", "PUT", "PATCH", "DELETE")
# routes whose side effect would end the probing session or need a real pairing; they are checked with a fresh session
SESSION_ENDERS = {("POST", "/api/logout"), ("DELETE", "/api/account/sessions")}


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        c.st = st
        c.routes_ = app.routes
        c.uids = {role: st.accounts.create("user-" + role, "a-long-test-password", role=role) for role in access.ROLES}
        yield c


def http_routes(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
                yield method, r.path


def concrete(path: str) -> str:
    def fill(m):
        name = m.group(1).split(":")[0]
        return "999999" if name.endswith("_id") else "x1"
    return re.sub(r"\{([^}]+)\}", fill, path)


def call(c, method, path, role, fresh=True):
    st = c.st
    token = st.sessions.create(c.uids[role], ip="127.0.0.1", agent="test")
    if not fresh:
        st.store.execute("UPDATE sessions SET stepup_until=0 WHERE token_hash=?", (auth._hash_token(token),))
    c.cookies.clear()
    c.cookies.set(auth.SESSION_COOKIE, token)
    try:
        return c.request(method, concrete(path), headers=HDR, json={})
    finally:
        c.cookies.clear()


def test_table_names_exactly_the_routes_of_the_app(app_client):
    routes = set(http_routes(app_client.routes_))
    table = {k for k in access.RULES if k[0] != "WS"}
    assert routes - table == set(), "routes without a rule: %s" % sorted(routes - table)
    assert table - routes == set(), "rules for routes that do not exist: %s" % sorted(table - routes)
    ws = {("WS", r.path) for r in app_client.routes_ if isinstance(r, APIWebSocketRoute)}
    assert ws == {k for k in access.RULES if k[0] == "WS"}


def test_every_rule_is_well_formed():
    for key, rule in access.RULES.items():
        assert rule.role in access.ROLES + ("public", "executor", "agent"), key
        if key[0] in CHANGING and rule.role not in ("executor", "agent"):
            # public reads of codes (pairing hello/status, passkey challenge, invite check) change nothing worth a line
            if rule.role == "public" and key[1] in ("/api/passkey/login/options", "/api/invite/check", "/api/ai/pair/hello",
                                                    "/api/ai/pair/status"):
                continue
            if key in (("POST", "/api/account/stepup/options"), ("POST", "/api/passkey/register/options"),
                       ("POST", "/api/account/totp/setup")):
                continue        # only hand out a challenge or a not yet active secret; the confirming call is logged
            assert rule.audit, "state-changing route without an audit action: %s %s" % key
        if rule.stepup:
            assert rule.role in access.ROLES, key


@pytest.mark.parametrize("role", access.ROLES)
def test_every_route_against_the_table(app_client, role):
    wrong = []
    for method, path in http_routes(app_client.routes_):
        rule = access.RULES[(method, path)]
        if rule.role in ("public", "executor", "agent") or (method, path) in SESSION_ENDERS:
            continue
        r = call(app_client, method, path, role)
        refused = r.status_code == 403 and r.json().get("error") == "auth.err.role"
        should_refuse = not access.allows(role, rule.role)
        if refused != should_refuse:
            wrong.append((method, path, role, r.status_code, r.text[:80]))
    assert wrong == []


def test_session_enders_follow_the_table(app_client):
    for key in SESSION_ENDERS:
        assert access.RULES[key].role == "viewer"
        r = call(app_client, key[0], key[1], "viewer")
        assert r.status_code == 200, key


def test_step_up_routes_refuse_an_old_confirmation(app_client):
    checked = 0
    for (method, path), rule in access.RULES.items():
        if not rule.stepup or method == "WS":
            continue
        role = rule.role
        r = call(app_client, method, path, role, fresh=False)
        assert r.status_code == 403 and r.json()["error"] == "auth.err.stepup", (method, path, r.text)
        assert r.json()["methods"] == ["password"]          # this user has no second factor yet
        r = call(app_client, method, path, role, fresh=True)
        assert r.json().get("error") != "auth.err.stepup", (method, path)
        checked += 1
    assert checked >= 10


def test_admin_routes_cover_users_credentials_executor_ai_updates():
    admin = {p for (m, p), r in access.RULES.items() if r.role == "admin"}
    for must in ("/api/users", "/api/users/invite", "/api/security", "/api/audit", "/api/audit/export", "/api/integrations/test",
                 "/api/firewall/approve", "/api/ai", "/api/ai/local/install", "/api/update/apply", "/api/reports/settings"):
        assert must in admin, must
    assert access.RULES[("PUT", "/api/settings")].role == "admin"
    assert access.RULES[("POST", "/api/integrations")].stepup and access.RULES[("POST", "/api/users/invite")].stepup


def test_operator_can_draft_rename_and_scan():
    for key in (("PUT", "/api/firewall/draft"), ("PATCH", "/api/devices/{device_id}"), ("POST", "/api/scan"),
                ("PUT", "/api/reports/{day}/items")):
        assert access.RULES[key].role == "operator", key


def test_unknown_route_is_for_admins_only(app_client):
    @app_client.app.get("/api/not-in-the-table")
    async def extra():
        return {"ok": True}
    routes = app_client.app.router.routes
    routes.insert(0, routes.pop())
    assert call(app_client, "GET", "/api/not-in-the-table", "operator").json()["error"] == "auth.err.role"
    assert call(app_client, "GET", "/api/not-in-the-table", "admin").status_code == 200


def test_changes_and_refusals_end_up_in_the_audit_log(app_client):
    st = app_client.st
    dev = app_client.st.core.devices()
    r = call(app_client, "PUT", "/api/settings", "operator")
    assert r.status_code == 403
    token = st.sessions.create(app_client.uids["operator"], ip="127.0.0.1", agent="test")
    app_client.cookies.set(auth.SESSION_COOKIE, token)
    r = app_client.post("/api/scan", json={"cidrs": ["192.0.2.0/24"]}, headers=HDR)
    app_client.cookies.clear()
    rows = st.audit.query({}, 50)
    acts = [(e["action"], e["outcome"], e["actor"]) for e in rows]
    assert ("settings.change", "denied", "user-operator") in acts
    assert any(a == "scan.start" and who == "user-operator" for a, _, who in acts)
    assert st.audit.verify()["ok"]
    assert dev is not None


def test_viewer_sees_no_admin_only_fields():
    data = {"name": "Router", "location": {"cell_id": 1}, "items": [{"cell_id": 7, "rssi": -60}]}
    assert access.redact_admin_fields(data, "viewer") == {"name": "Router", "items": [{"rssi": -60}]}
    assert access.redact_admin_fields(data, "operator") == {"name": "Router", "items": [{"rssi": -60}]}
    assert access.redact_admin_fields(data, "admin") == data


def test_websocket_needs_a_full_session(app_client):
    from starlette.websockets import WebSocketDisconnect
    st = app_client.st
    token = st.sessions.create(app_client.uids["viewer"], stage="enroll")
    app_client.cookies.set(auth.SESSION_COOKIE, token)
    with pytest.raises(WebSocketDisconnect) as exc:
        with app_client.websocket_connect("/api/live", headers={"Origin": "http://testserver"}) as ws:
            ws.receive_text()
    assert exc.value.code == 4401
    app_client.cookies.clear()


def test_enrolment_session_reaches_only_the_enrolment_routes(app_client):
    st = app_client.st
    token = st.sessions.create(app_client.uids["admin"], stage="enroll")
    app_client.cookies.set(auth.SESSION_COOKIE, token)
    assert app_client.get("/api/devices").json()["error"] == "auth.err.enroll"
    assert app_client.get("/api/account").status_code == 200
    assert app_client.post("/api/account/totp/setup", json={}, headers=HDR).status_code == 200
    app_client.cookies.clear()
    allowed = {k for k, r in access.RULES.items() if r.enroll}
    assert allowed <= {("GET", "/api/state"), ("GET", "/api/i18n/{lang}"), ("POST", "/api/logout"), ("GET", "/api/account"),
                       ("POST", "/api/account/totp/setup"), ("POST", "/api/account/totp/enable"), ("GET", "/api/passkeys"),
                       ("POST", "/api/passkey/register/options"), ("POST", "/api/passkey/register/verify")}


def test_every_audit_action_has_a_label_in_every_language():
    import json
    import os
    i18n = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lanloom", "i18n")
    for name in sorted(os.listdir(i18n)):
        if name.endswith(".json") and not name.startswith("board."):
            with open(os.path.join(i18n, name), encoding="utf-8") as f:
                texts = json.load(f)
            missing = sorted(a for a in access.AUDIT_ACTIONS if "audit.action." + a not in texts)
            assert not missing, "%s: %s" % (name, missing)
