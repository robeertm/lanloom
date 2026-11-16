"""Users, roles and second factor end to end: TOTP with a fixed clock, sign-in in two steps, lock-out notices,
invitations and reset links, last administrator, sessions, public computer, step-up, trusted auth proxy, sub-path,
audit chain against tampering, and no secret in the audit log, its export or the notices."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import access, auth, totp
from lanloom import config as config_mod
from lanloom.audit import AuditLog, scrub
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.main import create_app
from lanloom.store import Store

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"      # base32 of "12345678901234567890" (RFC 6238 appendix B)  secret-scan: fake


# ------------------------------------------------------------------ TOTP (RFC 6238) with a fixed clock
@pytest.mark.parametrize("t, code8", [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
                                      (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130")])
def test_rfc6238_vectors(t, code8):
    assert totp.code_at(RFC_SECRET, t) == code8[-6:]


def test_window_is_one_step_either_side():
    t = 1_700_000_000
    now = totp.code_at(RFC_SECRET, t)
    assert totp.verify(RFC_SECRET, now, 0, t) == totp.step_of(t)
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, t - 30), 0, t) == totp.step_of(t) - 1
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, t + 30), 0, t) == totp.step_of(t) + 1
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, t - 60), 0, t) is None
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, t + 60), 0, t) is None
    assert totp.verify(RFC_SECRET, "12345", 0, t) is None and totp.verify(RFC_SECRET, "abcdef", 0, t) is None
    assert totp.verify(RFC_SECRET, now[:3] + " " + now[3:], 0, t) == totp.step_of(t)     # spaces are fine


def test_a_code_cannot_be_used_twice():
    t = 1_700_000_000
    code = totp.code_at(RFC_SECRET, t)
    step = totp.verify(RFC_SECRET, code, 0, t)
    assert totp.verify(RFC_SECRET, code, step, t) is None                 # same code again
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, t - 30), step, t) is None   # an older one neither


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def factors(clock):
    store = Store(":memory:")
    vault = Vault(store, b"k" * KEY_BYTES)
    acc = auth.Accounts(store)
    uid = acc.create("alice", PW, role="admin")
    return store, vault, auth.SecondFactor(store, vault, b"p" * 32, clock=clock), uid


def test_enable_check_and_reuse_lock_with_a_fixed_clock():
    clock = Clock(1_800_000_000.0)
    store, vault, f, uid = factors(clock)
    secret = f.begin(uid)
    assert f.enable(uid, "000000" if totp.code_at(secret, clock.t) != "000000" else "111111") is None
    codes = f.enable(uid, totp.code_at(secret, clock.t))
    assert len(codes) == 10 and len(set(codes)) == 10 and f.has_totp(uid)
    # the enabling code is spent; the next step's code works once
    assert not f.check_totp(uid, totp.code_at(secret, clock.t))
    clock.t += 30
    assert f.check_totp(uid, totp.code_at(secret, clock.t))
    assert not f.check_totp(uid, totp.code_at(secret, clock.t))
    clock.t += 31
    assert f.check_totp(uid, totp.code_at(secret, clock.t - 30)) is False   # older than the last accepted step
    # the secret is stored encrypted, never in clear text in the database
    dump = "\n".join(str(r) for r in store.query("SELECT * FROM secrets") + store.query("SELECT * FROM users"))
    assert secret not in dump


def test_pending_secret_expires():
    clock = Clock(1_800_000_000.0)
    _, _, f, uid = factors(clock)
    secret = f.begin(uid)
    clock.t += auth.TOTP_PENDING_S + 1
    assert f.enable(uid, totp.code_at(secret, clock.t)) is None


def test_recovery_codes_are_hashed_and_single_use():
    clock = Clock(1_800_000_000.0)
    store, _, f, uid = factors(clock)
    secret = f.begin(uid)
    codes = f.enable(uid, totp.code_at(secret, clock.t))
    rows = store.query("SELECT code_hash FROM recovery_codes WHERE user_id=?", (uid,))
    assert len(rows) == 10
    for c in codes:
        assert all(c.replace("-", "") not in r["code_hash"] for r in rows)
    assert f.check_recovery(uid, codes[0].upper().replace("-", " "))       # case and separators do not matter
    assert not f.check_recovery(uid, codes[0])
    assert f.recovery_left(uid) == 9
    new = f.new_recovery(uid)
    assert not f.check_recovery(uid, codes[1]) and f.check_recovery(uid, new[1])


# ------------------------------------------------------------------ app fixtures
def make_app(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return create_app(config_mod.load())


@pytest.fixture()
def c(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://lanloom.test") as client:
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        r = client.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR)
        assert r.status_code == 200
        client.st = app.state.ll
        yield client


def login(c, user="admin", pw=PW, **extra):
    c.cookies.clear()
    return c.post("/api/login", json=dict({"username": user, "password": pw}, **extra), headers=HDR)


def enable_totp(c):
    s = c.post("/api/account/totp/setup", json={}, headers=HDR).json()
    assert s["uri"].startswith("otpauth://totp/Lanloom%3Aadmin?secret=") and "<svg" in s["qr"]
    r = c.post("/api/account/totp/enable", json={"code": totp.code_at(s["secret"])}, headers=HDR)
    assert r.status_code == 200, r.text
    return s["secret"], r.json()["recovery_codes"]


def next_code(c, secret, uid):
    """A code newer than the last accepted step (the test runs within one 30-second step)."""
    c.st.store.execute("UPDATE users SET totp_step=0 WHERE id=?", (uid,))
    return totp.code_at(secret)


# ------------------------------------------------------------------ first account, migration
def test_first_account_is_admin_with_a_grace_period(c):
    me = c.get("/api/account").json()
    assert me["role"] == "admin" and me["mfa"]["required"] and not me["mfa"]["overdue"]
    assert me["mfa"]["due"] > time.time() + 47 * 3600
    st = c.get("/api/state").json()
    assert st["can"] == {"role": "admin", "operate": True, "admin": True}


def test_existing_account_becomes_admin_on_upgrade(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES('schema','1');
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
          pw_hash TEXT NOT NULL, user_handle BLOB NOT NULL, created REAL NOT NULL);
        CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created REAL NOT NULL,
          last_seen REAL NOT NULL, expires REAL NOT NULL);
        INSERT INTO users(username, pw_hash, user_handle, created) VALUES('owner', 'x', x'00', 1);
        INSERT INTO sessions VALUES('h', 1, 1, 1, 9999999999);""")
    con.commit()
    con.close()
    store = Store(str(db))
    assert store.one("SELECT role, source, disabled FROM users WHERE username='owner'") == {"role": "admin", "source": "local", "disabled": 0}
    assert store.one("SELECT sid FROM sessions")["sid"]
    store.close()
    again = Store(str(db))           # the migration runs once: a later viewer stays a viewer
    again.execute("INSERT INTO users(username, pw_hash, user_handle, created) VALUES('later', 'x', x'00', 2)")
    again.close()
    assert Store(str(db)).one("SELECT role FROM users WHERE username='later'")["role"] == "viewer"


# ------------------------------------------------------------------ sign-in with a second factor
def test_sign_in_in_two_steps(c):
    uid = c.st.accounts.by_name("admin")["id"]
    secret, codes = enable_totp(c)
    r = login(c)
    second = r.json()["second"]
    assert r.status_code == 200 and r.json()["ok"] is False and "ll_session" not in r.cookies
    assert set(second["methods"]) == {"totp", "recovery"}
    assert c.get("/api/devices").status_code == 401
    bad = c.post("/api/login/second", json={"ticket": second["ticket"], "code": "000000" if totp.code_at(secret) != "000000" else "111111"}, headers=HDR)
    assert bad.status_code == 401 and bad.json()["error"] == "auth.err.code"
    ok = c.post("/api/login/second", json={"ticket": second["ticket"], "code": next_code(c, secret, uid)}, headers=HDR)
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert c.get("/api/devices").status_code == 200
    assert c.get("/api/account").json()["method"] == "password+totp"
    # a ticket is single use
    again = c.post("/api/login/second", json={"ticket": second["ticket"], "code": "123456"}, headers=HDR)
    assert again.json()["error"] == "auth.err.ticket"
    # recovery code instead of the app: works once, leaves a notice
    t2 = login(c).json()["second"]["ticket"]
    assert c.post("/api/login/second", json={"ticket": t2, "kind": "recovery", "code": codes[3]}, headers=HDR).json()["ok"]
    kinds = [n["kind"] for n in c.get("/api/account/notices").json()["notices"]]
    assert "recovery_used" in kinds
    t3 = login(c).json()["second"]["ticket"]
    assert c.post("/api/login/second", json={"ticket": t3, "kind": "recovery", "code": codes[3]}, headers=HDR).status_code == 401


def test_ticket_dies_after_five_wrong_codes_and_is_bound_to_the_client(c):
    secret, _ = enable_totp(c)
    ticket = login(c).json()["second"]["ticket"]
    for _ in range(auth.TICKET_TRIES):
        c.post("/api/login/second", json={"ticket": ticket, "code": "000001"}, headers=HDR)
    c.st.limiter = auth.LoginLimiter()        # the rate limit is not what is tested here
    r = c.post("/api/login/second", json={"ticket": ticket, "code": totp.code_at(secret)}, headers=HDR)
    assert r.json()["error"] in ("auth.err.ticket", "auth.err.rate")
    assert c.st.tickets.get(login(c).json()["second"]["ticket"], "198.51.100.9") is None     # other client


def test_lockout_notices_reach_the_user_and_the_admins(c):
    st = c.st
    st.accounts.create("bob", PW, role="viewer")
    for _ in range(5):
        login(c, "bob", "wrong-password-123")
    assert login(c, "bob", PW).status_code == 429
    bob = st.accounts.by_name("bob")
    admin = st.accounts.by_name("admin")
    assert [n["kind"] for n in st.notices.unseen(bob["id"])] == ["lockout"]
    assert "lockout" in [n["kind"] for n in st.notices.unseen(admin["id"])]
    acts = [(e["action"], e["actor"]) for e in st.audit.query({}, 50)]
    assert ("auth.lockout", "bob") in acts and acts.count(("auth.login_failed", "bob")) == 5
    # a name that does not exist is never written down
    st.limiter = auth.LoginLimiter()
    login(c, "S3cret-typed-into-the-name-field", "x" * 12)
    assert all("S3cret" not in json.dumps(e) for e in st.audit.query({}, 50))


def test_new_address_or_device_leaves_a_notice(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, LANLOOM_TRUSTED_PROXIES="10.9.0.2")
    with TestClient(peer(app, "10.9.0.2"), base_url="http://lanloom.test") as c:
        st = app.state.ll
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        hdr = dict(HDR, **{"X-Forwarded-For": "192.0.2.10", "User-Agent": "curl/8"})
        assert c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=hdr).status_code == 200
        # same address, another browser: no notice (the setup counted as the first sign-in)
        c.cookies.clear()
        c.post("/api/login", json={"username": "admin", "password": PW}, headers=dict(hdr, **{"User-Agent": "TestBrowser/140 (desktop)"}))
        assert c.get("/api/account/notices", headers=hdr).json()["notices"] == []
        # another address with a browser never seen: "new device"
        c.cookies.clear()
        other = dict(HDR, **{"X-Forwarded-For": "198.51.100.7", "User-Agent": "TestBrowser/9 (Mobile)"})
        c.post("/api/login", json={"username": "admin", "password": PW}, headers=other)
        notes = c.get("/api/account/notices", headers=other).json()["notices"]
        assert [n["kind"] for n in notes] == ["new_device"] and notes[0]["data"]["ip"] == "198.51.100.7"
        # another address, known browser: "new address"
        c.cookies.clear()
        third = dict(HDR, **{"X-Forwarded-For": "203.0.113.4", "User-Agent": "TestBrowser/9 (Mobile)"})
        c.post("/api/login", json={"username": "admin", "password": PW}, headers=third)
        assert [n["kind"] for n in c.get("/api/account/notices", headers=third).json()["notices"]] == ["new_address", "new_device"]
        assert c.post("/api/account/notices/seen", json={}, headers=third).json()["seen"] == 2
        assert c.get("/api/state", headers=third).json()["notices"] == 0
        assert st.audit.verify()["ok"]


def test_public_computer_gets_a_short_session_without_a_lasting_cookie(c):
    r = login(c, public_computer=True)
    cookie = r.headers["set-cookie"]
    assert "Max-Age" not in cookie and "expires" not in cookie.lower() and "HttpOnly" in cookie
    sess = c.get("/api/account/sessions").json()["sessions"]
    mine = [s for s in sess if s["current"]][0]
    assert mine["public_computer"] is True
    row = c.st.store.one("SELECT created, expires FROM sessions WHERE sid=?", (mine["id"],))
    assert row["expires"] - row["created"] == auth.FOREIGN_HOURS * 3600
    # idle for longer than the short idle time: gone
    c.st.store.execute("UPDATE sessions SET last_seen=? WHERE sid=?", (time.time() - auth.FOREIGN_IDLE_MIN * 60 - 5, mine["id"]))
    assert c.get("/api/devices").status_code == 401


def test_enrolment_after_the_grace_period(c):
    st = c.st
    uid = st.accounts.by_name("admin")["id"]
    st.store.execute("UPDATE users SET mfa_due=? WHERE id=?", (time.time() - 1, uid))
    r = login(c)
    assert r.json() == {"ok": True, "enroll": True}
    assert c.get("/api/state").json()["enroll"] is True
    assert c.get("/api/devices").json()["error"] == "auth.err.enroll"
    secret, _ = enable_totp(c)                       # setting up the factor turns the session into a full one
    assert c.get("/api/devices").status_code == 200
    assert c.get("/api/account").json()["method"] == "password+totp"


def test_policy_per_role_is_adjustable(c):
    st = c.st
    st.accounts.create("op", PW, role="operator")
    r = c.put("/api/security", json={"mfa_required": {"operator": True}, "mfa_grace_hours": 0, "audit_retention_days": 90}, headers=HDR)
    assert r.status_code == 200, r.text
    assert r.json()["policy"]["mfa_required"] == {"viewer": False, "operator": True, "admin": True}
    assert r.json()["retention"] == 90
    assert login(c, "op").json() == {"ok": True, "enroll": True}       # grace 0: straight to the set-up page
    assert c.put("/api/security", json={"mfa_grace_hours": 5}, headers=HDR).status_code in (401, 403)
    login(c)
    assert c.put("/api/security", json={"mfa_grace_hours": 5}, headers=HDR).json()["error"] == "security.err.grace"
    assert c.put("/api/security", json={"audit_retention_days": 12}, headers=HDR).json()["error"] == "security.err.retention"


# ------------------------------------------------------------------ step-up
def test_step_up_with_totp_and_password(c):
    st = c.st
    uid = st.accounts.by_name("admin")["id"]
    st.store.execute("UPDATE sessions SET stepup_until=0")
    r = c.post("/api/users/invite", json={"role": "viewer"}, headers=HDR)
    assert r.status_code == 403 and r.json() == {"error": "auth.err.stepup", "methods": ["password"]}
    assert c.post("/api/account/stepup", json={"kind": "password", "password": "wrong-password-1"}, headers=HDR).status_code == 401
    assert c.post("/api/account/stepup", json={"kind": "password", "password": PW}, headers=HDR).json()["ok"]
    assert c.post("/api/users/invite", json={"role": "viewer"}, headers=HDR).status_code == 200
    secret, codes = enable_totp(c)
    st.store.execute("UPDATE sessions SET stepup_until=0")
    r = c.post("/api/users/invite", json={"role": "viewer"}, headers=HDR)
    assert r.json()["methods"] == ["totp", "recovery"]
    # with a second factor the password alone no longer counts
    assert c.post("/api/account/stepup", json={"kind": "password", "password": PW}, headers=HDR).status_code == 401
    assert c.post("/api/account/stepup", json={"kind": "totp", "code": next_code(c, secret, uid)}, headers=HDR).json()["ok"]
    assert c.post("/api/users/invite", json={"role": "viewer"}, headers=HDR).status_code == 200


# ------------------------------------------------------------------ invitations, reset links, users
def test_invitation_is_single_use_bound_to_its_role_and_expires(c):
    r = c.post("/api/users/invite", json={"role": "operator", "note": "tablet"}, headers=HDR).json()
    token = r["token"]
    assert r["expires"] - time.time() == pytest.approx(auth.INVITE_HOURS * 3600, abs=5)
    assert c.post("/api/invite/check", json={"token": token}, headers=HDR).json()["role"] == "operator"
    admin_cookies = dict(c.cookies)
    c.cookies.clear()
    r = c.post("/api/invite/accept", json={"token": token, "username": "olga", "password": PW, "role": "admin"}, headers=HDR)
    assert r.status_code == 200 and r.json()["ok"]
    me = c.get("/api/account").json()
    assert me["role"] == "operator" and me["username"] == "olga"           # the role of the link, not of the request
    c.cookies.clear()
    assert c.post("/api/invite/accept", json={"token": token, "username": "olga2", "password": PW}, headers=HDR).status_code == 404
    for k, v in admin_cookies.items():
        c.cookies.set(k, v)
    old = c.post("/api/users/invite", json={"role": "viewer"}, headers=HDR).json()
    c.st.store.execute("UPDATE invites SET expires=? WHERE id=?", (time.time() - 1, old["id"]))
    assert c.post("/api/invite/check", json={"token": old["token"]}, headers=HDR).status_code == 404
    # only the hash of a token is stored
    assert all(token not in json.dumps(r) for r in c.st.store.query("SELECT * FROM invites"))


def test_reset_link_sets_a_new_password_and_can_remove_the_second_factor(c):
    st = c.st
    uid = st.accounts.create("bob", PW, role="viewer")
    st.vault.put("totp:%d" % uid, totp.new_secret())
    st.sessions.create(uid)
    r = c.post("/api/users/%d/reset" % uid, json={"clear_2fa": True}, headers=HDR).json()
    admin_cookies = dict(c.cookies)
    c.cookies.clear()
    info = c.post("/api/invite/check", json={"token": r["token"]}, headers=HDR).json()
    assert info["kind"] == "reset" and info["username"] == "bob" and info["clear_2fa"]
    assert c.post("/api/invite/accept", json={"token": r["token"], "password": "short"}, headers=HDR).json()["error"] == "auth.err.password_short"
    r2 = c.post("/api/invite/accept", json={"token": r["token"], "password": "a-brand-new-password"}, headers=HDR)
    assert r2.json() == {"ok": True, "login": True, "username": "bob"}
    assert not st.factors.has_totp(uid) and st.sessions.list(uid) == []
    assert login(c, "bob", "a-brand-new-password").json()["ok"] is True
    assert login(c, "bob", PW).status_code == 401
    assert "password_reset" in [n["kind"] for n in st.notices.unseen(uid)]
    for k, v in admin_cookies.items():
        c.cookies.set(k, v)


def test_last_admin_cannot_be_removed_or_demoted(c):
    st = c.st
    me = st.accounts.by_name("admin")["id"]
    assert c.patch("/api/users/%d" % me, json={"role": "viewer"}, headers=HDR).json()["error"] == "users.err.last_admin"
    assert c.patch("/api/users/%d" % me, json={"disabled": True}, headers=HDR).json()["error"] in ("users.err.last_admin", "users.err.self")
    assert c.delete("/api/users/%d" % me, headers=HDR).json()["error"] == "users.err.self"
    other = st.accounts.create("second", PW, role="admin")
    assert c.patch("/api/users/%d" % me, json={"role": "operator"}, headers=HDR).status_code == 200
    # now an operator: the users page is closed for this session at once
    assert c.get("/api/users").json()["error"] == "auth.err.role"
    assert st.accounts.get(other)["role"] == "admin"


def test_disabling_a_user_ends_the_sessions_and_sign_in(c):
    st = c.st
    uid = st.accounts.create("carl", PW, role="operator")
    token = st.sessions.create(uid)
    assert c.patch("/api/users/%d" % uid, json={"disabled": True}, headers=HDR).json()["disabled"] is True
    assert st.sessions.resolve(token) is None
    assert login(c, "carl").status_code == 401


def test_sessions_list_and_sign_out_everywhere(c):
    st = c.st
    uid = st.accounts.by_name("admin")["id"]
    other = st.sessions.create(uid, ip="198.51.100.4", agent="phone")
    sess = c.get("/api/account/sessions").json()["sessions"]
    assert len(sess) == 2 and sum(s["current"] for s in sess) == 1
    foreign = [s for s in sess if not s["current"]][0]
    assert foreign["agent"] == "phone"
    assert c.delete("/api/account/sessions/" + foreign["id"], headers=HDR).json()["ok"]
    assert st.sessions.resolve(other) is None
    st.sessions.create(uid)
    r = c.delete("/api/account/sessions", headers=HDR)
    assert r.json()["ended"] == 2
    assert c.get("/api/devices").status_code == 401


def test_admin_can_end_the_sessions_of_another_user(c):
    st = c.st
    uid = st.accounts.create("dora", PW, role="viewer")
    tok = st.sessions.create(uid)
    assert len(c.get("/api/users/%d/sessions" % uid).json()["sessions"]) == 1
    assert c.delete("/api/users/%d/sessions" % uid, headers=HDR).json()["ended"] == 1
    assert st.sessions.resolve(tok) is None


def test_roles_take_effect_immediately_and_viewer_language_is_personal(c):
    st = c.st
    uid = st.accounts.create("eve", PW, role="viewer")
    login(c, "eve")
    assert c.put("/api/settings", json={"language": "de"}, headers=HDR).json()["error"] == "auth.err.role"
    assert c.put("/api/account/prefs", json={"language": "de"}, headers=HDR).json()["prefs"] == {"language": "de"}
    assert c.get("/api/state").json()["language"] == "de"
    assert st.store.get_setting("language", None) != "de"
    st.accounts.set_role(uid, "operator")
    assert c.get("/api/integrations").status_code == 200


# ------------------------------------------------------------------ trusted auth proxy and sub-path
def test_trusted_header_only_from_trusted_proxies(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, LANLOOM_TRUSTED_PROXIES="testclient,127.0.0.1", LANLOOM_AUTH_HEADER="Remote-User",
                   LANLOOM_AUTH_GROUPS_HEADER="Remote-Groups", LANLOOM_AUTH_GROUP_ROLES="net-admins=admin,net-ops=operator")
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        # the TestClient peer is "testclient" (not an address): not trusted, the header counts for nothing
        assert c.get("/api/devices", headers={"Remote-User": "mallory", "Remote-Groups": "net-admins"}).status_code == 401
        proxy = auth.ProxyAuth(config_mod.load(), auth.parse_proxies("10.9.0.2"), st)
        from starlette.datastructures import Headers
        h = Headers({"remote-user": "mallory", "remote-groups": "net-admins"})
        assert proxy.user("192.0.2.66", h) is None                          # not the proxy: ignored
        u = proxy.user("10.9.0.2", h)
        assert u["role"] == "admin" and u["source"] == "proxy"
        assert proxy.user("10.9.0.2", Headers({"remote-user": "trudy", "remote-groups": "guests"})) == {}   # no role: refused
        assert proxy.user("10.9.0.2", Headers({"remote-user": "mallory", "remote-groups": "net-ops"}))["role"] == "operator"
        assert st.accounts.by_name("mallory")["role"] == "operator"
        assert [e["action"] for e in st.audit.query({}, 10)][:2] == ["users.change", "users.joined"]
        assert proxy.user("10.9.0.2", Headers({})) is None                   # no header: normal session sign-in


def peer(app, address):
    """The app as seen from another network peer (the test client always comes from "testclient")."""
    async def wrapped(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope, client=(address, 4000))
        await app(scope, receive, send)
    return wrapped


def test_proxy_header_via_the_app_from_a_trusted_address(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, LANLOOM_TRUSTED_PROXIES="10.9.0.0/24", LANLOOM_AUTH_HEADER="X-Auth-User",
                   LANLOOM_AUTH_DEFAULT_ROLE="viewer")
    with TestClient(app, base_url="http://lanloom.test"):
        pass
    with TestClient(peer(app, "10.9.0.2"), base_url="http://lanloom.test") as c:
        r = c.get("/api/devices", headers={"X-Auth-User": "kim"})
        assert r.status_code == 200
        assert c.get("/api/state", headers={"X-Auth-User": "kim"}).json()["proxy"] is True
        assert c.put("/api/settings", json={}, headers=dict(HDR, **{"X-Auth-User": "kim"})).json()["error"] == "auth.err.role"
    with TestClient(peer(app, "192.0.2.5"), base_url="http://lanloom.test") as c:
        assert c.get("/api/devices", headers={"X-Auth-User": "kim"}).status_code == 401


def test_agent_label_names_a_kind_never_a_product():
    assert auth.agent_label("TestBrowser/9 (Mobile)") == "phone"
    assert auth.agent_label("TestBrowser/9 (Tablet)") == "tablet"
    assert auth.agent_label("TestBrowser/140 (desktop)") == "computer"
    assert auth.agent_label("curl/8.5") == "script" and auth.agent_label("") == "unknown"
    assert auth.device_key("a") != auth.device_key("b")


def test_sub_path(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, LANLOOM_BASE_PATH="/net/lanloom/")
    with TestClient(app, base_url="http://lanloom.test") as c:
        r = c.get("/net/lanloom", follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"] == "/net/lanloom/"
        assert c.get("/net/lanloom/api/health").json()["ok"]
        assert c.get("/api/health").json()["ok"]                       # a proxy that strips the prefix works too
        assert c.get("/net/lanloom/").text.startswith("<!doctype html>")
        assert c.get("/net/lanloom/api/devices").status_code == 401
    assert config_mod._base_path("../evil") == "" and config_mod._base_path("x y") == ""


# ------------------------------------------------------------------ audit log: chain, tampering, retention, export
def audit_log(clock=None):
    store = Store(":memory:")
    return store, AuditLog(store, b"a" * 32, clock=clock or time.time)


def test_chain_detects_changed_inserted_removed_and_cut_entries():
    store, log = audit_log()
    for i in range(6):
        log.record("settings.change", {"id": 1, "username": "admin", "role": "admin"}, detail={"n": i}, ip="192.0.2.1")
    assert log.verify() == {"ok": True, "count": 6, "broken_at": None, "reason": ""}
    # edited detail
    store.execute("UPDATE audit SET detail='{\"n\":99}' WHERE id=3")
    assert log.verify()["broken_at"] == 3 and log.verify()["reason"] == "audit.chain.changed"
    store, log = audit_log()
    for i in range(6):
        log.record("x.y", None, detail={"n": i})
    # removed in the middle
    store.execute("DELETE FROM audit WHERE id=4")
    assert log.verify()["reason"] == "audit.chain.gap"
    store, log = audit_log()
    for i in range(6):
        log.record("x.y", None, detail={"n": i})
    # re-computed with a plain hash (no key): still detected
    import hashlib
    row = store.one("SELECT * FROM audit WHERE id=6")
    store.execute("UPDATE audit SET actor='nobody', hash=? WHERE id=6", (hashlib.sha256(b"forged").hexdigest(),))
    assert log.verify()["reason"] == "audit.chain.changed"
    store.execute("UPDATE audit SET actor=?, hash=? WHERE id=6", (row["actor"], row["hash"]))
    assert log.verify()["ok"]
    # cut off at the end
    store.execute("DELETE FROM audit WHERE id=6")
    assert log.verify()["reason"] == "audit.chain.tail"


def test_retention_keeps_a_valid_chain():
    clock = Clock(1_800_000_000.0)
    store, log = audit_log(clock)
    store.set_setting("audit_retention_days", 30)
    for day in range(40):
        clock.t = 1_800_000_000.0 + day * 86400
        log.record("auth.login", None, detail={"day": day})
    log.prune(clock.t)                              # also runs by itself at most hourly while writing
    assert log.verify()["ok"] and log.verify()["count"] == 31
    assert store.one("SELECT MIN(id) AS m FROM audit")["m"] == 10
    log.record("auth.login", None)
    assert log.verify()["ok"]
    store.execute("DELETE FROM audit")              # everything gone, but the anchor remembers
    assert log.verify()["reason"] == "audit.chain.tail"


def test_filters_and_csv_export_without_formula_injection():
    store, log = audit_log()
    log.record("users.invite", {"id": 1, "username": "admin", "role": "admin"}, target="=HYPERLINK(1)", detail={"role": "viewer"})
    log.record("device.change", {"id": 2, "username": "op", "role": "operator"}, target="d-1")
    assert [e["action"] for e in log.query({"actor": "op"})] == ["device.change"]
    assert [e["action"] for e in log.query({"action": "users."})] == ["users.invite"]
    assert [e["action"] for e in log.query({"q": "HYPER"})] == ["users.invite"]
    text = log.export_csv({})
    assert text.splitlines()[0].startswith("id,time_utc,user,role,action")
    assert "'=HYPERLINK(1)" in text and ",=HYPERLINK" not in text


def test_scrub_hides_secret_names_and_limits_size():
    out = scrub({"password": "p", "api_key": "k", "nested": {"token": "t", "ok": "fine"}, "code": "123456",
                 "long": "x" * 500, "many": list(range(50)), "hidden_fields": ["password"]})
    assert out["password"] == out["api_key"] == out["nested"]["token"] == out["code"] == "[hidden]"
    assert out["nested"]["ok"] == "fine" and len(out["long"]) == 200 and len(out["many"]) == 21
    assert out["hidden_fields"] == ["password"]


def test_audit_view_and_export_through_the_app(c):
    r = c.get("/api/audit?limit=10").json()
    assert r["chain"]["ok"] and r["entries"][0]["action"] == "users.joined" and "admin" in r["users"]
    csv_r = c.get("/api/audit/export")
    assert csv_r.headers["content-type"].startswith("text/csv") and "attachment" in csv_r.headers["content-disposition"]
    assert c.get("/api/audit?limit=5").json()["entries"][0]["action"] == "audit.export"


# ------------------------------------------------------------------ no secret in the audit log, its export, notices or sessions
LEAK = "Leak-Marker-7f3a9q"


def test_no_secret_reaches_the_audit_log(c):
    st = c.st
    uid = st.accounts.by_name("admin")["id"]
    secret, codes = enable_totp(c)
    c.post("/api/account/password", json={"current": PW, "password": LEAK + "-pw-new"}, headers=HDR)
    c.post("/api/integrations/test", json={"kind": "snmp", "config": {"host": "192.0.2.1", "version": "2c"},
                                           "secrets": {"community": LEAK + "-community"}}, headers=HDR)
    c.post("/api/integrations", json={"kind": "snmp", "name": "Switch", "config": {"host": "192.0.2.1", "version": "2c"},
                                      "secrets": {"community": LEAK + "-community"}}, headers=HDR)
    c.put("/api/reports/settings", json={"settings": {"time": "06:30"}, "secrets": {"webhook_url": "https://example.invalid/" + LEAK}}, headers=HDR)
    c.put("/api/ai", json={"settings": {}, "secrets": {"api_key": LEAK + "-ai"}}, headers=HDR)
    inv = c.post("/api/users/invite", json={"role": "viewer", "note": "for the shop"}, headers=HDR).json()
    login(c, "admin", LEAK + "-pw-new")
    c.post("/api/login", json={"username": "admin", "password": LEAK + "-wrong"}, headers=HDR)
    ticket = login(c, "admin", LEAK + "-pw-new").json()["second"]["ticket"]
    c.post("/api/login/second", json={"ticket": ticket, "code": next_code(c, secret, uid)}, headers=HDR)
    c.post("/api/account/stepup", json={"kind": "recovery", "code": codes[0]}, headers=HDR)
    everything = json.dumps(st.store.query("SELECT * FROM audit")) + c.get("/api/audit/export").text \
        + json.dumps(st.store.query("SELECT * FROM notices")) + json.dumps(st.store.query("SELECT * FROM sessions"))
    for needle in (LEAK, secret, inv["token"], codes[0], codes[0].replace("-", "")):
        assert needle not in everything, needle
    assert st.audit.verify()["ok"]
    actions = {e["action"] for e in st.audit.query({}, 200)}
    assert {"account.totp_on", "account.password", "integration.test", "integration.save", "reports.settings", "ai.settings",
            "users.invite", "auth.login", "auth.login_failed", "auth.stepup"} <= actions


def test_demo_shows_sample_users_that_cannot_sign_in(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, LANLOOM_DEMO_SEED="1")
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        assert st.store.user_count() == 0 and (Path(tmp_path) / "setup-code.txt").is_file()
        roles = {u["username"]: u["role"] for u in st.accounts.all()}
        assert roles == {"guest": "viewer", "night-shift": "operator"}
        assert login(c, "guest", "").status_code == 401 and login(c, "night-shift", "!sample").status_code == 401
        assert st.audit.verify()["ok"] and len(st.audit.query({}, 50)) >= 5
        assert access.ROLES == ("viewer", "operator", "admin")
