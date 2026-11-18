"""API tokens (lanloom/apitokens.py) and how the guard treats them (lanloom/main.py token_request).

* The scope is never more than the account's role - when made and later (demoted, disabled, deleted).
* Only a hash is stored; the token is in exactly one answer; it never shows up in a list, the audit log, a backup,
  a diagnostics export or the log (leak test).
* Expired and revoked tokens are refused, each token has its own request limit, "last used" is kept.
* A token opens only /api/v1; the routes of the web page never accept one, and the page's cross-site checks do not
  apply to a request that carries a token (it has no cookie).
"""
import json
import logging

import pytest
from fastapi.testclient import TestClient

from lanloom import access, apitokens, auth
from lanloom import config as config_mod
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}


@pytest.fixture()
def cl(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        c.st = app.state.ll
        c.uids = {r: c.st.accounts.create("user-" + r, "a-long-test-password", role=r) for r in access.ROLES}
        yield c


def sign_in(c, role, fresh=True):
    token = c.st.sessions.create(c.uids[role], ip="127.0.0.1", agent="test")
    if not fresh:
        c.st.store.execute("UPDATE sessions SET stepup_until=0 WHERE token_hash=?", (auth._hash_token(token),))
    c.cookies.clear()
    c.cookies.set(auth.SESSION_COOKIE, token)


def make(c, role, scope, **extra):
    sign_in(c, role)
    r = c.post("/api/tokens", json=dict({"name": "%s-%s" % (role, scope), "scope": scope}, **extra), headers=HDR)
    c.cookies.clear()
    assert r.status_code == 200, r.text
    return r.json()


def bearer(tok):
    return {"Authorization": "Bearer " + (tok["token"] if isinstance(tok, dict) else tok)}


def test_scope_never_above_the_role(cl):
    for role, allowed in (("viewer", {"read"}), ("operator", {"read", "operate", "ingest"}), ("admin", set(apitokens.SCOPES))):
        sign_in(cl, role)
        assert set(cl.get("/api/tokens").json()["scopes"]) == allowed
        for scope in apitokens.SCOPES:
            r = cl.post("/api/tokens", json={"name": "t-" + scope, "scope": scope}, headers=HDR)
            assert (r.status_code == 200) == (scope in allowed), (role, scope, r.text)
            if r.status_code != 200:
                assert r.json()["error"] == "api.err.scope_role"


def test_making_a_token_needs_a_fresh_confirmation_and_the_page_header(cl):
    sign_in(cl, "admin", fresh=False)
    r = cl.post("/api/tokens", json={"name": "x", "scope": "read"}, headers=HDR)
    assert r.status_code == 403 and r.json()["error"] == "auth.err.stepup"
    sign_in(cl, "admin")
    assert cl.post("/api/tokens", json={"name": "x", "scope": "read"}).status_code == 403          # no X-Lanloom header


def test_bad_input_is_refused(cl):
    sign_in(cl, "admin")
    for body, key in (({"name": "", "scope": "read"}, "api.err.name"), ({"name": "x", "scope": "root"}, "api.err.scope"),
                      ({"name": "x", "scope": "read", "expires_days": 5000}, "api.err.expiry"),
                      ({"name": "x", "scope": "read", "expires_days": 0}, "api.err.expiry"),
                      ({"name": "x", "scope": "read", "rate": 99999}, "api.err.rate")):
        r = cl.post("/api/tokens", json=body, headers=HDR)
        assert r.status_code == 400 and r.json()["error"] == key, (body, r.text)


def test_only_a_hash_is_stored_and_the_token_is_shown_once(cl):
    tok = make(cl, "operator", "read")
    assert tok["token"].startswith("llt_") and len(tok["token"]) >= 40 and tok["prefix"] == tok["token"][:12]
    row = cl.st.store.one("SELECT * FROM api_tokens WHERE id=?", (tok["id"],))
    assert row["token_hash"] == apitokens.hash_token(tok["token"])
    assert tok["token"] not in json.dumps(row)
    sign_in(cl, "operator")
    listed = cl.get("/api/tokens").json()
    assert tok["token"] not in json.dumps(listed) and listed["tokens"][0]["prefix"] == tok["prefix"]
    assert "token" not in listed["tokens"][0] and "token_hash" not in listed["tokens"][0]


def test_a_token_reads_the_public_api_with_its_scope(cl):
    tok = make(cl, "admin", "read")
    r = cl.get("/api/v1", headers=bearer(tok))
    assert r.status_code == 200
    assert r.json()["you"] == {"username": "user-admin", "role": "viewer", "via": "token", "token": "admin-read", "scope": "read"}
    assert cl.get("/api/v1/devices", headers=bearer(tok)).status_code == 200
    r = cl.post("/api/v1/ingest", json={"source": "s", "title": "t"}, headers=bearer(tok))
    assert r.status_code == 403 and r.json() == {"error": "api.err.scope", "need": "operate"}


def test_a_token_never_opens_the_routes_of_the_web_page(cl):
    tok = make(cl, "admin", "admin")
    for method, path in (("GET", "/api/devices"), ("GET", "/api/tokens"), ("POST", "/api/tokens"), ("GET", "/api/users"),
                         ("PUT", "/api/settings"), ("GET", "/api/webhooks"), ("GET", "/api/audit")):
        r = cl.request(method, path, headers=dict(bearer(tok), **HDR), json={"name": "x", "scope": "read"})
        assert r.status_code == 401 and r.json()["error"] == "api.err.v1_only", (method, path)


def test_a_token_wins_over_a_cookie_and_needs_no_page_header(cl):
    tok = make(cl, "operator", "ingest")
    sign_in(cl, "admin")                       # an administrator's session cookie travels along
    r = cl.get("/api/v1/devices", headers=bearer(tok))
    assert r.status_code == 403                # the ingest token decides, not the cookie
    cl.cookies.clear()
    r = cl.post("/api/v1/ingest", json={"source": "s", "title": "t"}, headers=bearer(tok))     # no X-Lanloom, no Origin
    assert r.status_code == 200, r.text


def test_unknown_expired_and_revoked_tokens_are_refused(cl):
    assert cl.get("/api/v1", headers=bearer("llt_" + "x" * 43)).json()["error"] == "api.err.token"
    r = cl.get("/api/v1", headers=bearer("llt_" + "x" * 43))
    assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"
    tok = make(cl, "viewer", "read")
    cl.st.store.execute("UPDATE api_tokens SET expires=? WHERE id=?", (1.0, tok["id"]))
    assert cl.get("/api/v1", headers=bearer(tok)).json()["error"] == "api.err.token_expired"
    tok2 = make(cl, "viewer", "read")
    assert cl.get("/api/v1", headers=bearer(tok2)).status_code == 200
    sign_in(cl, "viewer")
    assert cl.delete("/api/tokens/%d" % tok2["id"], headers=HDR).status_code == 200
    cl.cookies.clear()
    assert cl.get("/api/v1", headers=bearer(tok2)).status_code == 401


def test_the_account_caps_the_token_later(cl):
    tok = make(cl, "operator", "operate")
    ok = cl.post("/api/v1/ingest", json={"source": "s", "title": "t"}, headers=bearer(tok))
    assert ok.status_code == 200
    cl.st.store.execute("UPDATE users SET role='viewer' WHERE id=?", (cl.uids["operator"],))
    assert cl.get("/api/v1", headers=bearer(tok)).json()["you"]["role"] == "viewer"
    assert cl.post("/api/v1/ingest", json={"source": "s", "title": "t"}, headers=bearer(tok)).status_code == 403
    cl.st.store.execute("UPDATE users SET disabled=1 WHERE id=?", (cl.uids["operator"],))
    assert cl.get("/api/v1", headers=bearer(tok)).status_code == 401
    cl.st.store.execute("UPDATE users SET disabled=0 WHERE id=?", (cl.uids["operator"],))
    cl.st.store.execute("DELETE FROM users WHERE id=?", (cl.uids["operator"],))
    assert cl.get("/api/v1", headers=bearer(tok)).status_code == 401
    assert cl.st.store.one("SELECT COUNT(*) AS n FROM api_tokens")["n"] == 0          # went with the account


def test_ingest_scope_reaches_only_the_ingest_route_and_needs_an_operator(cl):
    tok = make(cl, "admin", "ingest")
    for path in ("/api/v1", "/api/v1/devices", "/api/v1/ingest", "/api/v1/openapi.json"):
        assert cl.get(path, headers=bearer(tok)).status_code == 403, path
    assert cl.post("/api/v1/ingest", json={"source": "s", "title": "t"}, headers=bearer(tok)).status_code == 200
    cl.st.store.execute("UPDATE users SET role='viewer' WHERE id=?", (cl.uids["admin"],))
    assert cl.post("/api/v1/ingest", json={"source": "s", "title": "t"}, headers=bearer(tok)).status_code == 403


def test_each_token_has_its_own_request_limit(cl):
    tok = make(cl, "viewer", "read", rate=60)
    other = make(cl, "viewer", "read", rate=60)
    for _ in range(60):
        assert cl.get("/api/v1", headers=bearer(tok)).status_code == 200
    r = cl.get("/api/v1", headers=bearer(tok))
    assert r.status_code == 429 and r.json()["error"] == "api.err.rate" and int(r.headers["retry-after"]) >= 1
    assert cl.get("/api/v1", headers=bearer(other)).status_code == 200           # the other token is not affected


def test_last_used_and_uses_are_kept(cl):
    tok = make(cl, "viewer", "read")
    cl.get("/api/v1", headers=bearer(tok))
    row = cl.st.store.one("SELECT last_used, last_ip, uses FROM api_tokens WHERE id=?", (tok["id"],))
    assert row["last_used"] and row["last_ip"] and row["uses"] >= 1


def test_revoking_other_peoples_tokens_is_for_administrators(cl):
    tok = make(cl, "operator", "read")
    sign_in(cl, "viewer")
    assert cl.delete("/api/tokens/%d" % tok["id"], headers=HDR).status_code == 404
    sign_in(cl, "admin")
    others = cl.get("/api/tokens").json()["others"]
    assert [o["owner"] for o in others] == ["user-operator"]
    assert cl.delete("/api/tokens/%d" % tok["id"], headers=HDR).status_code == 200
    sign_in(cl, "operator")
    assert cl.get("/api/tokens").json()["others"] is None


def test_changes_through_a_token_are_in_the_audit_log_without_the_token(cl, caplog):
    caplog.set_level(logging.DEBUG)
    tok = make(cl, "operator", "operate")
    cl.post("/api/v1/ingest", json={"source": "probe-src", "title": "t"}, headers=bearer(tok))
    cl.post("/api/v1/ingest", json={"source": "s", "title": "t"}, headers=bearer(make(cl, "viewer", "read")))     # denied
    rows = cl.st.store.query("SELECT action, actor, outcome, target, detail FROM audit WHERE action LIKE 'api.%' ORDER BY id")
    acts = [(r["action"], r["outcome"]) for r in rows]
    assert ("api.token_create", "ok") in acts and ("api.ingest", "ok") in acts and ("api.ingest", "denied") in acts
    ok = next(r for r in rows if r["action"] == "api.ingest" and r["outcome"] == "ok")
    assert ok["target"] == "probe-src" and json.loads(ok["detail"])["key_name"] == "operator-operate"
    # leak test: the token never appears in the audit log, its export, a diagnostics export, a backup or the log
    sign_in(cl, "admin")
    blobs = [json.dumps(cl.st.store.query("SELECT * FROM audit")), cl.get("/api/audit/export").text,
             cl.post("/api/diagnostics", json={}, headers=HDR).text, caplog.text]
    from lanloom import diagnostics
    blobs.append(json.dumps(diagnostics.build(cl.st), default=str))
    content = cl.st.backups.collect(history=True)
    assert content["tables"]["api_tokens"][0]["token_hash"]           # tokens move with a backup - as hashes only
    blobs.append(json.dumps(content, default=str))
    for blob in blobs:
        assert tok["token"] not in blob
        assert tok["token"][4:] not in blob


def test_unknown_routes_under_v1_are_404_and_guessing_is_capped(cl):
    tok = make(cl, "viewer", "read")
    r = cl.get("/api/v1/nothing-here", headers=bearer(tok))
    assert r.status_code == 404 and r.json()["error"] == "api.err.not_found"
    from lanloom import flood
    codes = [cl.get("/api/v1", headers=bearer("llt_" + "%043d" % i)).status_code for i in range(flood.PUBLIC_LIMIT + 2)]
    assert codes[0] == 401 and codes[-1] == 429


def test_the_bearer_tokens_of_agents_and_the_executor_are_not_taken_for_api_tokens(cl):
    r = cl.post("/api/agent/report", json={}, headers=dict(HDR, Authorization="Bearer llt_" + "x" * 43))
    assert r.status_code == 401 and r.json()["error"] == "auth.err.required"
