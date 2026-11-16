"""Backup, restore and moving to another host (lanloom/backup.py, lanloom/web_backup.py).

* File format: round trip, wrong passphrase, damaged file, foreign file, newer container/content/version refused,
  crafted key-derivation parameters refused before any memory is spent.
* Round trip through the HTTP API between two instances with different instance keys (a move): accounts with
  password, one-time codes and recovery codes, credentials, manual inventory entries, "connected via", drafts, audit
  log; sessions end, the target keeps its own key.
* Older contents are migrated, the schedule writes, prunes and reports, nothing secret leaks into files, names,
  logs, audit entries or error messages.
"""
import json
import logging
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

from lanloom import auth, backup, totp
from lanloom import config as config_mod
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}
PASS = "correct horse battery staple"
SECRET = "integration-secret-value-8231"       # secret-scan: fake (test value)
REPORT_SECRET = "report-mail-password-5531"     # secret-scan: fake (test value)
PW = "a-long-test-password"


@pytest.fixture(autouse=True)
def quick_kdf(monkeypatch):
    # the real parameters (64 MiB, 3 passes) are covered by test_default_kdf_parameters; tests run with small ones
    monkeypatch.setattr(backup, "KDF_DEFAULT", {"m": 8192, "t": 1, "p": 1})


def make_app(tmp_path, name, monkeypatch, demo=False):
    d = tmp_path / name
    monkeypatch.setenv("LANLOOM_DATA", str(d))
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    if demo:
        monkeypatch.setenv("LANLOOM_DEMO", "1")
    else:
        monkeypatch.delenv("LANLOOM_DEMO", raising=False)
    app = create_app(config_mod.load())
    return app


def sign_in(c, st, uid):
    token = st.sessions.create(uid, ip="127.0.0.1", agent="test")
    c.cookies.set(auth.SESSION_COOKIE, token)
    return token


def fill(st):
    """An instance with everything a backup must carry."""
    uid = st.accounts.create("admin", PW, role="admin")
    viewer = st.accounts.create("viewer-one", PW, role="viewer")
    secret = totp.new_secret()
    st.vault.put("totp:%d" % uid, secret)
    codes = st.factors.new_recovery(uid)
    st.store.set_setting("setup_done", True)
    st.store.set_setting("language", "de")
    st.store.set_setting("rescan_hours", 12)
    st.store.set_setting("fw_draft:%d" % uid, {"moves": {"d10": "n_work"}, "t": 1, "undo_of": None})
    st.store.set_setting("update_cache", {"local": "runtime only"})
    iid = st.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES('mqtt', 'Broker', '{\"host\":\"192.168.50.5\"}', 1, ?)",
                           (time.time(),)).lastrowid
    st.vault.put("int:%d:password" % iid, SECRET)
    st.vault.put("report:mail:password", REPORT_SECRET)
    st.vault.put("totp_pending:%d" % viewer, json.dumps({"s": "X", "t": 0}))
    now = time.time()
    for i, (dev_id, title) in enumerate((("d-a", "Printer"), ("d-b", None), ("d-c", None))):
        st.store.execute("INSERT INTO devices(id, ip, mac, data, kind, confidence, title, user_title, user_kind, hidden, first_seen, last_seen)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (dev_id, "192.168.50.%d" % (10 + i), "02:4c:4c:00:00:%02x" % i, "{}", "unknown", 0.4, "host-%d" % i,
                          title, "printer" if title else None, 1 if dev_id == "d-c" else 0, now, now))
    st.store.execute("INSERT INTO phys_manual(device_id, parent_id, port, medium, updated) VALUES('d-a', 'd-b', '3', 'cable', ?)", (now,))
    st.store.execute("INSERT INTO events(t, kind, data) VALUES(?, 'wan_down', '{}')", (now - 60,))
    st.audit.record("settings.change", {"username": "admin", "id": uid, "role": "admin"}, detail={"fields": ["language"]})
    return {"uid": uid, "viewer": viewer, "totp": secret, "codes": codes, "integ": iid}


# ---------------------------------------------------------------------- file format
def test_default_kdf_parameters_are_strong():
    from lanloom import backup as real
    assert real.KDF_LIMITS["m"][0] <= 65536 <= real.KDF_LIMITS["m"][1]
    assert real.PASSPHRASE_MIN >= 12


def test_seal_and_open_round_trip_and_refusals():
    content = {"format": backup.CONTENT_FORMAT, "app": backup.__version__, "tables": {"users": [{"id": 1}]}, "x": "ü"}
    blob = backup.seal(content, backup.new_kek(PASS), {"app": backup.__version__, "format": backup.CONTENT_FORMAT, "created": 1})
    header, back = backup.open_sealed(blob, PASS)
    assert back["x"] == "ü" and header["kdf"]["alg"] == "argon2id"
    with pytest.raises(backup.BackupError) as e:
        backup.open_sealed(blob, PASS + "!")
    assert e.value.key == "backup.err.wrong_passphrase"
    for pos in (len(blob) - 3, len(blob) - 40):                       # content and tag
        bad = bytearray(blob)
        bad[pos] ^= 0x10
        with pytest.raises(backup.BackupError) as e:
            backup.open_sealed(bytes(bad), PASS)
        assert e.value.key == "backup.err.damaged"
    with pytest.raises(backup.BackupError) as e:
        backup.open_sealed(blob[:len(blob) // 2], PASS)
    assert e.value.key == "backup.err.damaged"
    with pytest.raises(backup.BackupError) as e:
        backup.open_sealed(b"PK\x03\x04 a zip file, not a backup", PASS)
    assert e.value.key == "backup.err.not_backup"


def test_header_changes_are_noticed():
    blob = backup.seal({"format": 1, "app": backup.__version__}, backup.new_kek(PASS), {"app": backup.__version__, "format": 1, "created": 1})
    header, prefix = backup.read_header(blob)
    header["meta"]["history"] = True                       # same length change: flip a flag inside the authenticated header
    h2 = json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
    import struct
    forged = backup.MAGIC + struct.pack(">BI", 1, len(h2)) + h2 + blob[len(prefix):]
    with pytest.raises(backup.BackupError) as e:
        backup.open_sealed(forged, PASS)
    assert e.value.key == "backup.err.damaged"


def test_crafted_kdf_parameters_are_refused_without_deriving(monkeypatch):
    blob = backup.seal({"format": 1, "app": backup.__version__}, backup.new_kek(PASS), {"app": backup.__version__, "format": 1})
    header, prefix = backup.read_header(blob)
    header["kdf"]["m"] = 64 * 1024 * 1024                  # 64 GiB
    h2 = json.dumps(header).encode()
    import struct
    forged = backup.MAGIC + struct.pack(">BI", 1, len(h2)) + h2 + blob[len(prefix):]
    monkeypatch.setattr(backup, "derive", lambda *a, **k: pytest.fail("must not derive"))
    with pytest.raises(backup.BackupError) as e:
        backup.open_sealed(forged, PASS)
    assert e.value.key == "backup.err.damaged"


def test_newer_backups_are_refused_with_the_version(monkeypatch):
    newer = "%d.0.0" % (backup.version_tuple(backup.__version__)[0] + 1)
    blob = backup.seal({"format": 1, "app": newer}, backup.new_kek(PASS), {"app": newer, "format": 1})
    assert backup.public_meta(backup.read_header(blob)[0])["compat"] == "newer"
    with pytest.raises(backup.BackupError) as e:
        backup.open_sealed(blob, PASS)
    assert e.value.key == "backup.err.newer" and e.value.vars["version"] == newer
    # a header claiming an old version does not help: the content says newer
    blob = backup.seal({"format": 1, "app": newer}, backup.new_kek(PASS), {"app": "1.0.0", "format": 1})
    with pytest.raises(backup.BackupError) as e:
        backup.open_sealed(blob, PASS)
    assert e.value.key == "backup.err.newer"
    blob = backup.seal({"format": backup.CONTENT_FORMAT + 1, "app": backup.__version__}, backup.new_kek(PASS),
                       {"app": backup.__version__, "format": backup.CONTENT_FORMAT + 1})
    with pytest.raises(backup.BackupError) as e:
        backup.open_sealed(blob, PASS)
    assert e.value.key == "backup.err.newer"
    import struct
    future = backup.MAGIC + struct.pack(">BI", backup.FILE_VERSION + 1, 2) + b"{}"
    with pytest.raises(backup.BackupError) as e:
        backup.open_sealed(future, PASS)
    assert e.value.key == "backup.err.newer_file"


def test_older_contents_are_migrated(monkeypatch):
    # content written before roles existed: the account becomes an administrator with the grace period
    old = {"format": 1, "app": "1.11.1", "tables": {"users": [{"id": 1, "username": "admin", "pw_hash": "x", "user_handle": {"$b": "AAAA"},
                                                              "created": 1.0}]}}
    blob = backup.seal(old, backup.new_kek(PASS), {"app": "1.11.1", "format": 1})
    assert backup.public_meta(backup.read_header(blob)[0])["compat"] == "older"
    _, content = backup.open_sealed(blob, PASS)
    u = content["tables"]["users"][0]
    assert u["role"] == "admin" and u["mfa_due"] > time.time()
    # the step chain: a format-1 content is lifted to a (pretended) format 2
    monkeypatch.setattr(backup, "CONTENT_FORMAT", 2)
    monkeypatch.setitem(backup.MIGRATIONS, 1, lambda c: dict(c, lifted=True))
    _, content = backup.open_sealed(blob, PASS)
    assert content["lifted"] and content["format"] == 2
    monkeypatch.delitem(backup.MIGRATIONS, 1)
    with pytest.raises(backup.BackupError) as e:
        backup.open_sealed(blob, PASS)
    assert e.value.key == "backup.err.too_old"


def test_passphrase_rules():
    for bad in ("", "short", None, 12345):
        with pytest.raises(backup.BackupError):
            backup.check_passphrase(bad)
    assert backup.check_passphrase("x" * 12)


# ---------------------------------------------------------------------- the move: export on A, restore on a fresh B
def test_round_trip_to_a_fresh_instance_through_the_setup_page(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    app_a = make_app(tmp_path, "a", monkeypatch)
    with TestClient(app_a, base_url="http://lanloom.test") as c:
        st = app_a.state.ll
        data = fill(st)
        sign_in(c, st, data["uid"])
        before = c.get("/api/backup").json()
        assert before["estimate"]["config_bytes"] > 0 and before["estimate"]["history_bytes"] > 0
        r = c.post("/api/backup/export", headers=HDR, json={"passphrase": PASS, "history": True})
        assert r.status_code == 200, r.text
        blob = r.content
        name = r.headers["x-lanloom-file"]
        key_a = (tmp_path / "a" / "secret.key").read_bytes()
        audit_a = st.store.one("SELECT COUNT(*) AS n FROM audit")["n"]
        devices_a = st.store.query("SELECT id, user_title, user_kind, hidden FROM devices ORDER BY id")
    # nothing readable in the file or its name
    for needle in (SECRET, REPORT_SECRET, PASS, "admin", "viewer-one", "192.168.50", "Printer", data["totp"]):
        assert needle.encode() not in blob, needle
        assert needle not in name
    assert name.startswith("lanloom-") and name.endswith(".llbackup")

    app_b = make_app(tmp_path, "b", monkeypatch)
    with TestClient(app_b, base_url="http://lanloom.test") as c:
        stb = app_b.state.ll
        code = stb.setup_code.ensure()
        assert stb.store.get_setting("setup_done", False) is False
        r = c.post("/api/setup/restore/upload", headers=dict(HDR, **{"X-Lanloom-Setup-Code": "WRNG-CODE", "Content-Type": "application/octet-stream"}), content=blob)
        assert r.status_code == 403
        r = c.post("/api/setup/restore/upload", headers=dict(HDR, **{"X-Lanloom-Setup-Code": code, "Content-Type": "application/octet-stream"}), content=blob)
        assert r.status_code == 200, r.text
        assert r.json()["meta"]["compat"] == "same" and r.json()["meta"]["history"] is True
        r = c.post("/api/setup/restore/check", headers=HDR, json={"code": code, "passphrase": "not the passphrase"})
        assert r.status_code == 400 and r.json()["error"] == "backup.err.wrong_passphrase"
        assert "not the passphrase" not in r.text
        r = c.post("/api/setup/restore/check", headers=HDR, json={"code": code, "passphrase": PASS})
        assert r.status_code == 200, r.text
        pv = r.json()
        assert [u["username"] for u in pv["users"]["new"]] == ["admin", "viewer-one"] and pv["users"]["now"] == []
        assert pv["integrations"]["new"] == ["Broker"] and pv["devices"]["manual_new"] == 2 and pv["connections"]["new"] == 1
        assert pv["drafts"]["new"] == 1 and pv["audit"]["imported"] is True and pv["history"]["included"] is True
        r = c.post("/api/setup/restore", headers=HDR, json={"code": code, "passphrase": PASS})
        assert r.status_code == 200, r.text
        assert r.json()["users"] == ["admin", "viewer-one"]
        # the set-up routes are closed now
        r = c.post("/api/setup/restore/check", headers=HDR, json={"code": code, "passphrase": PASS})
        assert r.status_code == 409
        assert c.get("/api/state").json()["setup_needed"] is False
        # the target has its own key; every secret was encrypted again with it
        assert (tmp_path / "b" / "secret.key").read_bytes() != key_a
        assert stb.vault.get("int:%d:password" % data["integ"]) == SECRET
        assert stb.vault.get("report:mail:password") == REPORT_SECRET
        assert stb.vault.get("totp_pending:%d" % data["viewer"]) is None          # half-finished set-ups stay behind
        # accounts work: password, one-time code, recovery code (the pepper moved along)
        assert stb.accounts.verify("admin", PW)
        assert stb.factors.check_totp(data["uid"], totp.code_at(data["totp"]))
        assert stb.factors.check_recovery(data["uid"], data["codes"][0])
        assert not stb.factors.check_recovery(data["uid"], data["codes"][0])     # used once
        # sign in for real, with the second factor
        r = c.post("/api/login", headers=HDR, json={"username": "admin", "password": PW})
        assert r.status_code == 200 and r.json().get("second")
        # inventory, manual entries, connected-via, drafts, settings
        assert stb.store.query("SELECT id, user_title, user_kind, hidden FROM devices ORDER BY id") == devices_a
        assert stb.store.one("SELECT parent_id, port FROM phys_manual WHERE device_id='d-a'") == {"parent_id": "d-b", "port": "3"}
        assert stb.store.get_setting("fw_draft:%d" % data["uid"])["moves"] == {"d10": "n_work"}
        assert stb.store.get_setting("language") == "de" and stb.store.get_setting("rescan_hours") == 12
        assert stb.store.get_setting("update_cache") is None                      # runtime state of A stays behind
        assert stb.store.one("SELECT COUNT(*) AS n FROM events")["n"] >= 1
        # the audit log moved along, is a valid chain under B's key and ends with the restore
        chain = stb.audit.verify()
        assert chain["ok"], chain
        last = stb.store.one("SELECT action, detail FROM audit ORDER BY id DESC LIMIT 1")
        assert last["action"] == "backup.restored"
        assert stb.store.one("SELECT COUNT(*) AS n FROM audit")["n"] >= audit_a + 1
        assert stb.store.one("SELECT COUNT(*) AS n FROM sessions")["n"] == 0
        dump = json.dumps(stb.store.query("SELECT * FROM audit"))
        for needle in (PASS, SECRET, REPORT_SECRET):
            assert needle not in dump
    for rec in caplog.records:
        for needle in (PASS, SECRET, REPORT_SECRET, "not the passphrase"):
            assert needle not in rec.getMessage()


def test_restore_in_place_needs_confirmation_ends_sessions_and_keeps_the_audit_log(tmp_path, monkeypatch):
    app = make_app(tmp_path, "a", monkeypatch)
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        data = fill(st)
        sign_in(c, st, data["uid"])
        blob = c.post("/api/backup/export", headers=HDR, json={"passphrase": PASS}).content
        # change things after the backup
        st.store.execute("UPDATE devices SET user_title='Renamed later' WHERE id='d-a'")
        st.store.execute("DELETE FROM phys_manual")
        newer = st.accounts.create("added-later", PW, role="viewer")
        audit_before = st.store.one("SELECT COUNT(*) AS n FROM audit")["n"]
        up = c.post("/api/backup/upload", headers=dict(HDR, **{"Content-Type": "application/octet-stream"}), content=blob).json()
        assert up["meta"]["history"] is False
        pv = c.post("/api/backup/check", headers=HDR, json={"upload": up["upload"], "passphrase": PASS}).json()
        assert "added-later" in [u["username"] for u in pv["users"]["now"]]
        assert "added-later" not in [u["username"] for u in pv["users"]["new"]]
        assert pv["audit"]["imported"] is False and pv["sessions_end"] >= 1
        r = c.post("/api/backup/restore", headers=HDR, json={"upload": up["upload"], "passphrase": PASS})
        assert r.status_code == 400 and r.json()["error"] == "backup.err.confirm"
        r = c.post("/api/backup/restore", headers=HDR, json={"upload": up["upload"], "passphrase": PASS, "confirm": True})
        assert r.status_code == 200, r.text
        assert r.json()["signed_out"] is True
        # the session that restored is gone
        assert c.get("/api/backup").status_code == 401
        assert st.store.one("SELECT user_title FROM devices WHERE id='d-a'")["user_title"] == "Printer"
        assert st.store.one("SELECT COUNT(*) AS n FROM phys_manual")["n"] == 1
        assert st.accounts.get(newer) is None
        # the instance kept its own audit log (a restore cannot wipe what happened) and wrote the restore into it
        rows = st.store.query("SELECT action FROM audit ORDER BY id")
        assert len(rows) >= audit_before + 1 and rows[-1]["action"] == "backup.restored"
        assert st.audit.verify()["ok"]
        # a safety copy of the database was taken first, private to the app
        copies = [p for p in (tmp_path / "a" / backup.FOLDER).iterdir() if p.name.startswith("before-restore-")]
        assert copies and stat.S_IMODE(copies[0].stat().st_mode) == 0o600


def test_wrong_passphrases_are_rate_limited(tmp_path, monkeypatch):
    app = make_app(tmp_path, "a", monkeypatch)
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        data = fill(st)
        sign_in(c, st, data["uid"])
        blob = c.post("/api/backup/export", headers=HDR, json={"passphrase": PASS}).content
        up = c.post("/api/backup/upload", headers=dict(HDR, **{"Content-Type": "application/octet-stream"}), content=blob).json()
        codes = [c.post("/api/backup/check", headers=HDR, json={"upload": up["upload"], "passphrase": "guess number %d" % i}).status_code
                 for i in range(6)]
        assert codes[:5] == [400] * 5 and codes[5] == 429
        # the right passphrase is refused as well while the lock-out lasts
        assert c.post("/api/backup/check", headers=HDR, json={"upload": up["upload"], "passphrase": PASS}).status_code == 429


def test_foreign_and_damaged_uploads(tmp_path, monkeypatch):
    app = make_app(tmp_path, "a", monkeypatch)
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        data = fill(st)
        sign_in(c, st, data["uid"])
        r = c.post("/api/backup/upload", headers=dict(HDR, **{"Content-Type": "application/octet-stream"}), content=b"hello world, not a backup")
        assert r.status_code == 400 and r.json()["error"] == "backup.err.not_backup"
        blob = bytearray(c.post("/api/backup/export", headers=HDR, json={"passphrase": PASS}).content)
        blob[-10] ^= 0xFF
        up = c.post("/api/backup/upload", headers=dict(HDR, **{"Content-Type": "application/octet-stream"}), content=bytes(blob)).json()
        r = c.post("/api/backup/check", headers=HDR, json={"upload": up["upload"], "passphrase": PASS})
        assert r.status_code == 400 and r.json()["error"] == "backup.err.damaged"
        r = c.post("/api/backup/check", headers=HDR, json={"upload": "../../etc", "passphrase": PASS})
        assert r.status_code == 404
        r = c.get("/api/backup/files/..%2Fsecret.key")
        assert r.status_code in (403, 404)
        assert c.delete("/api/backup/files/secret.key", headers=HDR).status_code == 404
        assert (tmp_path / "a" / "secret.key").exists()


def test_history_is_optional(tmp_path, monkeypatch):
    app = make_app(tmp_path, "a", monkeypatch)
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        fill(st)
        _, blob = st.backups.export(PASS, history=False)
        _, content = st.backups.check(blob, PASS)
        assert "events" not in content["tables"] and content["history"] is False
        events = st.store.one("SELECT COUNT(*) AS n FROM events")["n"]
        st.backups.restore(content)
        # history of the instance is left alone when the backup has none
        assert st.store.one("SELECT COUNT(*) AS n FROM events")["n"] >= events


# ---------------------------------------------------------------------- schedule and nightly report
def test_schedule_needs_a_key_writes_private_files_and_prunes(tmp_path, monkeypatch):
    app = make_app(tmp_path, "a", monkeypatch)
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        data = fill(st)
        sign_in(c, st, data["uid"])
        r = c.put("/api/backup/settings", headers=HDR, json={"enabled": True})
        assert r.status_code == 400 and r.json()["error"] == "backup.err.no_key"
        r = c.put("/api/backup/settings", headers=HDR, json={"enabled": True, "every_hours": 24, "keep": 2, "passphrase": "short"})
        assert r.status_code == 400 and r.json()["error"] == "backup.err.passphrase_short"
        r = c.put("/api/backup/settings", headers=HDR, json={"enabled": True, "every_hours": 24, "keep": 2, "passphrase": PASS})
        assert r.status_code == 200, r.text
        assert r.json()["key_set"] and r.json()["schedule"]["keep"] == 2
        assert PASS not in json.dumps(st.store.query("SELECT * FROM settings"))
        assert PASS not in json.dumps(st.store.query("SELECT * FROM audit"))
        assert st.backups.due()
        names = []
        clock = [time.time()]
        st.backups.clock = lambda: clock[0]
        for i in range(3):
            clock[0] += 3600
            r = c.post("/api/backup/run", headers=HDR, json={})
            assert r.status_code == 200, r.text
            names.append(r.json()["name"])
        files = [f["name"] for f in c.get("/api/backup").json()["files"]]
        assert files == [names[2], names[1]]                       # newest first, the oldest pruned
        p = tmp_path / "a" / backup.FOLDER / names[2]
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
        # a scheduled file opens with the passphrase (the app kept only the derived key)
        _, content = st.backups.check(p.read_bytes(), PASS)
        assert content["secrets"]["int:%d:password" % data["integ"]] == SECRET
        assert not st.backups.due()
        # download needs the admin; the file comes back unchanged
        r = c.get("/api/backup/files/" + names[2])
        assert r.status_code == 200 and r.content == p.read_bytes()
        assert st.store.one("SELECT action FROM audit WHERE action='backup.download'")
        # restore straight from the folder
        pv = c.post("/api/backup/check", headers=HDR, json={"file": names[2], "passphrase": PASS})
        assert pv.status_code == 200 and pv.json()["users"]["new"]
        assert c.delete("/api/backup/files/" + names[1], headers=HDR).status_code == 200
        assert not (tmp_path / "a" / backup.FOLDER / names[1]).exists()


def test_nightly_section_points_at_an_old_backup_and_a_failed_run(tmp_path, monkeypatch):
    from lanloom import backup_report
    from lanloom.nightly.contrib import Ctx
    app = make_app(tmp_path, "a", monkeypatch)
    with TestClient(app, base_url="http://lanloom.test"):
        st = app.state.ll
        fill(st)
        b = st.backups
        now = time.time()
        ctx = lambda: Ctx(core=st.core, journal=st.core.reports.journal, since=now - 86400, until=now, prev=None)
        sec = backup_report._build(ctx())
        assert [i["key"] for i in sec["items"]] == ["backup.report.off"] and sec["level"] == "ok"
        b.update_schedule({"enabled": True, "every_hours": 24, "keep": 3}, PASS)
        b.run_now()
        sec = backup_report._build(ctx())
        assert not any(i["point"] for i in sec["items"]) and sec["summary"]["key"] == "backup.report.summary"
        later = now + 3 * 86400
        ctx2 = Ctx(core=st.core, journal=st.core.reports.journal, since=later - 86400, until=later, prev=None)
        sec = backup_report._build(ctx2)
        assert any(i["key"] == "backup.report.stale" and i["point"] for i in sec["items"]) and sec["level"] == "warn"
        st.vault.delete(backup.KEK_SECRET)
        with pytest.raises(backup.BackupError):
            b.run_now()
        sec = backup_report._build(ctx())
        failed = [i for i in sec["items"] if i["key"] == "backup.report.failed"]
        assert failed and failed[0]["vars"]["reason_key"] == "backup.err.no_key"


def test_report_texts_and_error_texts_exist_in_every_language():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "lanloom"
    keys = set()
    for f in ("backup.py", "web_backup.py", "backup_report.py"):
        keys |= set(re.findall(r'"(backup\.(?:err|report)\.[a-z_]+)"', (root / f).read_text()))
    assert len(keys) > 15
    for lang in ("en", "de"):
        texts = json.loads((root / "i18n" / ("%s.json" % lang)).read_text())
        missing = sorted(k for k in keys if k not in texts)
        assert missing == [], (lang, missing)
        for action in ("backup.settings", "backup.export", "backup.run", "backup.delete", "backup.upload", "backup.check",
                       "backup.download", "backup.restored", "backup.restore_failed"):
            assert "audit.action." + action in texts, (lang, action)
        assert "notice.title.restored" in texts and "notice.text.restored" in texts


def test_demo_mode_shows_stored_backups(tmp_path, monkeypatch):
    app = make_app(tmp_path, "demo", monkeypatch, demo=True)
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        uid = st.accounts.create("admin", PW, role="admin")
        sign_in(c, st, uid)
        assert c.get("/api/backup").json()["files"] == []           # nothing before the demo network is set up
        st.store.set_setting("setup_done", True)
        state = c.get("/api/backup").json()
        assert len(state["files"]) == 3 and state["schedule"]["enabled"] and state["key_set"] and not state["stale"]
        newest = tmp_path / "demo" / backup.FOLDER / state["files"][0]["name"]
        _, content = st.backups.check(newest.read_bytes(), backup.Backups.DEMO_PASSPHRASE)
        assert content["tables"]["users"]
        assert len(c.get("/api/backup").json()["files"]) == 3       # seeded once
