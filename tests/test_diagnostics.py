"""The diagnostic export (lanloom/diagnostics.py): useful for a bug report, and clean.

* the document names version, installation, counts and the state of every source with its reason key;
* nothing identifying is in it: no hardware or IP address, no device, source or user name, no address of a connection,
  no secret - checked against every value of the (demo) inventory and against a connection with a recognisable name;
* the safety net ``redact`` replaces what looks like an address and drops free text, and says how often it did;
* administrators only; every download is in the audit log.
"""
import json
import re

import pytest
from fastapi.testclient import TestClient

from lanloom import access, auth, diagnostics
from lanloom import config as config_mod
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        c.st = st
        c.uids = {role: st.accounts.create("zz-name-" + role, "a-long-test-password", role=role) for role in access.ROLES}
        yield c


def as_role(c, role):
    token = c.st.sessions.create(c.uids[role], ip="127.0.0.1", agent="test")
    c.cookies.clear()
    c.cookies.set(auth.SESSION_COOKIE, token)


def test_redact_replaces_addresses_and_drops_free_text():
    doc = {"a": "seen 192.168.1.20 at aa:bb:cc:dd:ee:ff", "b": ["fe80::1ff:fe23:4567:890a", "me@example.org", "https://host.example/x?k=1"],
           "c": "My Living Room Hub!", "d": 7, "e": None, "f": {"192.168.0.1": "source.hub.timeout"}, "g": "1.29.1"}
    clean, n = diagnostics.redact(doc)
    text = json.dumps(clean)
    for secret in ("192.168", "aa:bb", "fe80", "example", "Living Room"):
        assert secret not in text, secret
    assert clean["g"] == "1.29.1" and clean["d"] == 7 and clean["e"] is None
    assert clean["c"] == "<removed>"
    assert n >= 6


def test_redact_keeps_reason_keys_and_versions():
    clean, n = diagnostics.redact({"reason": "source.snmp.timeout", "v": "1.29.1", "t": "2026-11-17T09:30Z", "kernel": "plainword"})
    assert n == 0 and clean["reason"] == "source.snmp.timeout" and clean["t"] == "2026-11-17T09:30Z"


def test_the_export_is_clean_and_useful(client):
    st = client.st
    # a connection whose name and address must not appear
    st.core.store.execute("INSERT INTO integrations (kind, name, config, enabled, created) VALUES (?,?,?,?,?)",
                          ("snmp", "Cellar Router Zeta", json.dumps({"host": "10.77.66.55", "community": "hunter2-community"}), 1, 1.0))
    as_role(client, "admin")
    r = client.post("/api/diagnostics", headers=HDR, json={})
    assert r.status_code == 200, r.text
    doc = r.json()
    text = r.text
    assert doc["format"] == diagnostics.FORMAT
    assert doc["version"]
    assert doc["devices"]["total"] >= 1 and doc["devices"]["per_kind"]
    assert doc["accounts"]["per_role"]["admin"] >= 1
    assert any(s["family"] == "snmp" and s["source"] == "snmp-1" for s in doc["sources"])
    assert doc["redaction"]["replaced"] == 0, "the sections themselves must already be clean"
    # nothing identifying, whatever the inventory holds
    forbidden = {"Cellar Router Zeta", "10.77.66.55", "hunter2-community", "zz-name-admin", "zz-name-viewer"}
    for dev in st.core.store.devices():
        forbidden.update(v for v in (dev["ip"], dev.get("mac")) if v)
        # a one-word title such as "Router" is also the name of a kind of device; a name with more in it must not appear
        forbidden.update(v for v in (dev["title"], dev.get("user_title")) if v and re.search(r"[\s\d_-]", v))
    leaked = sorted(f for f in forbidden if str(f).lower() in text.lower())
    assert not leaked, leaked
    assert not re.search(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", text, re.I)
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)


def test_a_failing_section_does_not_stop_the_export(client, monkeypatch):
    monkeypatch.setattr(diagnostics, "_devices", lambda core: 1 / 0)
    as_role(client, "admin")
    doc = client.post("/api/diagnostics", headers=HDR, json={}).json()
    assert doc["devices"] == {"unavailable": True}
    assert doc["runtime"]["python"]


def test_only_administrators_download_and_it_is_audited(client):
    for role in ("viewer", "operator"):
        as_role(client, role)
        assert client.post("/api/diagnostics", headers=HDR, json={}).status_code == 403
    as_role(client, "admin")
    assert client.post("/api/diagnostics", headers=HDR, json={}).status_code == 200
    actions = client.st.audit.actions()
    assert "diagnostics.export" in actions
    assert "diagnostics.export" in access.AUDIT_ACTIONS
