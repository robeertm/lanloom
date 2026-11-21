"""Accounts and passkeys stored under the OLD password and passkey libraries keep working after a library update.

tests/fixtures/auth_old_libs.json was written by tests/fixtures/auth_old_libs_make.py with argon2-cffi 23 and webauthn 2
(Lanloom's own code, base image of the Dockerfile). Here, under the libraries of requirements.txt:
  * the old password hash verifies, a wrong password does not, and the hash is NOT rewritten (no unwanted rehash);
  * the old passkey signs in (sign-in and step-up), its counter moves on, a replayed counter is refused;
  * a passkey registered now has the same stored form and signs in too;
  * the same over HTTP: passkey sign-in and password sign-in of the migrated account through the real routes."""
import json
from importlib import metadata
from pathlib import Path

import cbor2
import pytest
from fastapi.testclient import TestClient

from lanloom import auth
from lanloom import config as config_mod
from lanloom.main import create_app
from lanloom.store import Store
from softkey import ORIGIN, RP_ID, SoftKey, b64u

ROOT = Path(__file__).resolve().parent.parent
OLD = json.loads((ROOT / "tests" / "fixtures" / "auth_old_libs.json").read_text())
HDR = {"X-Lanloom": "1", "Origin": ORIGIN}


def old_store():
    store = Store(":memory:")
    u = OLD["user"]
    uid = store.execute("INSERT INTO users(username, pw_hash, user_handle, created, role) VALUES(?,?,?,?,?)",
                        (u["username"], u["pw_hash"], bytes.fromhex(u["user_handle"]), 1.0, u["role"])).lastrowid
    p = OLD["passkey"]
    store.execute("INSERT INTO passkeys(user_id, credential_id, public_key, sign_count, name, created) VALUES(?,?,?,?,?,?)",
                  (uid, bytes.fromhex(p["credential_id"]), bytes.fromhex(p["public_key"]), p["sign_count"], p["name"], 1.0))
    return store, uid


def old_key():
    return SoftKey(OLD["softkey_label"], counter=OLD["passkey"]["sign_count"])


def major(version: str) -> int:
    return int(version.split(".")[0])


def test_the_fixture_was_made_with_the_old_libraries_and_this_run_uses_newer_ones():
    made = OLD["made_with"]
    assert major(made["webauthn"]) == 2 and major(made["argon2-cffi"]) == 23
    assert major(metadata.version("webauthn")) > 2 and major(metadata.version("argon2-cffi")) > 23
    assert OLD["passkey"]["sign_count"] >= 1          # the stored passkey had already signed in once


# ------------------------------------------------------------------ argon2: verify, no rehash
def test_old_password_hash_verifies_and_is_not_rewritten():
    store, uid = old_store()
    acc = auth.Accounts(store)
    assert not auth._hasher.check_needs_rehash(OLD["user"]["pw_hash"])
    assert acc.verify(OLD["user"]["username"], OLD["password"])["id"] == uid
    assert store.one("SELECT pw_hash FROM users WHERE id=?", (uid,))["pw_hash"] == OLD["user"]["pw_hash"]
    assert acc.verify(OLD["user"]["username"], OLD["password"] + "x") is None
    assert acc.verify(OLD["user"]["username"], "") is None


def test_new_hashes_keep_the_parameters_of_the_old_ones():
    """Same algorithm and cost as before: a new hash would not make the old ones look outdated."""
    head = lambda h: h.rsplit("$", 2)[0]
    assert head(auth._hasher.hash("another-sample-password")) == head(OLD["user"]["pw_hash"])


# ------------------------------------------------------------------ passkeys stored with the old library
def test_old_passkey_signs_in_and_its_counter_moves_on():
    store, uid = old_store()
    pk = auth.Passkeys(store)
    key = old_key()
    opts = pk.login_options(RP_ID, ORIGIN)
    assert pk.login_verify(opts["ceremony"], key.sign_in(opts["options"], user_handle=bytes.fromhex(OLD["user"]["user_handle"]))) == uid
    row = store.one("SELECT sign_count, last_used FROM passkeys WHERE user_id=?", (uid,))
    assert row["sign_count"] == key.counter == OLD["passkey"]["sign_count"] + 1 and row["last_used"]


def test_old_passkey_works_for_step_up_and_not_for_another_user():
    store, uid = old_store()
    pk = auth.Passkeys(store)
    key = old_key()
    opts = pk.login_options(RP_ID, ORIGIN, user_id=uid)
    assert [c["id"] for c in opts["options"]["allowCredentials"]] == [b64u(key.credential_id)]
    assert pk.login_verify(opts["ceremony"], key.sign_in(opts["options"]), uid) == uid
    other = pk.login_options(RP_ID, ORIGIN, user_id=uid + 1)
    assert pk.login_verify(other["ceremony"], key.sign_in(other["options"]), uid + 1) is None


def test_a_replayed_counter_is_refused():
    store, uid = old_store()
    pk = auth.Passkeys(store)
    key = SoftKey(OLD["softkey_label"], counter=OLD["passkey"]["sign_count"] - 1)     # next signature repeats the stored count
    opts = pk.login_options(RP_ID, ORIGIN)
    assert pk.login_verify(opts["ceremony"], key.sign_in(opts["options"])) is None


def test_a_passkey_registered_now_is_stored_the_same_way_and_signs_in():
    store, uid = old_store()
    pk = auth.Passkeys(store)
    user = dict(store.one("SELECT * FROM users WHERE id=?", (uid,)))
    key = SoftKey("registered after the library update")
    reg = pk.registration_options(user, RP_ID, ORIGIN)
    assert [c["id"] for c in reg["options"]["excludeCredentials"]]          # the old passkey is not offered twice
    assert pk.registration_verify(uid, reg["ceremony"], key.register(reg["options"]), "Phone")
    rows = store.query("SELECT credential_id, public_key, sign_count FROM passkeys WHERE user_id=? ORDER BY id", (uid,))
    assert len(rows) == 2
    old_cose, new_cose = cbor2.loads(rows[0]["public_key"]), cbor2.loads(rows[1]["public_key"])
    assert sorted(old_cose) == sorted(new_cose) and old_cose[3] == new_cose[3] == -7
    assert bytes(rows[1]["credential_id"]) == key.credential_id and rows[1]["sign_count"] == 0
    opts = pk.login_options(RP_ID, ORIGIN)
    assert pk.login_verify(opts["ceremony"], key.sign_in(opts["options"])) == uid


def test_a_tampered_registration_is_refused():
    store, uid = old_store()
    pk = auth.Passkeys(store)
    user = dict(store.one("SELECT * FROM users WHERE id=?", (uid,)))
    reg = pk.registration_options(user, RP_ID, ORIGIN)
    answer = SoftKey("tampered").register(reg["options"], origin="http://elsewhere.example")
    assert not pk.registration_verify(uid, reg["ceremony"], answer, "x")
    assert store.one("SELECT COUNT(*) AS n FROM passkeys")["n"] == 1


# ------------------------------------------------------------------ the same through the routes
@pytest.fixture()
def migrated(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url=ORIGIN) as client:
        st = app.state.ll
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        assert client.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"},
                           headers=HDR).status_code == 200
        u, p = OLD["user"], OLD["passkey"]
        uid = st.store.execute("INSERT INTO users(username, pw_hash, user_handle, created, role) VALUES(?,?,?,?,?)",
                               (u["username"], u["pw_hash"], bytes.fromhex(u["user_handle"]), 1.0, u["role"])).lastrowid
        st.store.execute("INSERT INTO passkeys(user_id, credential_id, public_key, sign_count, name, created) VALUES(?,?,?,?,?,?)",
                         (uid, bytes.fromhex(p["credential_id"]), bytes.fromhex(p["public_key"]), p["sign_count"], p["name"], 1.0))
        client.cookies.clear()
        client.st, client.uid = st, uid
        yield client


def test_old_passkey_signs_in_through_the_routes(migrated):
    key = old_key()
    opts = migrated.post("/api/passkey/login/options", json={}, headers=HDR).json()
    r = migrated.post("/api/passkey/login/verify", headers=HDR, json={
        "ceremony": opts["ceremony"], "credential": key.sign_in(opts["options"], user_handle=bytes.fromhex(OLD["user"]["user_handle"]))})
    assert r.status_code == 200, r.text
    me = migrated.get("/api/state", headers=HDR).json()
    assert json.dumps(me).count(OLD["user"]["username"]) >= 1


def test_old_password_signs_in_through_the_routes_without_a_rehash(migrated):
    r = migrated.post("/api/login", json={"username": OLD["user"]["username"], "password": OLD["password"]}, headers=HDR)
    assert r.status_code == 200, r.text
    row = migrated.st.store.one("SELECT pw_hash FROM users WHERE id=?", (migrated.uid,))
    assert row["pw_hash"] == OLD["user"]["pw_hash"]
    migrated.cookies.clear()
    bad = migrated.post("/api/login", json={"username": OLD["user"]["username"], "password": "not-the-password"}, headers=HDR)
    assert bad.status_code == 401
