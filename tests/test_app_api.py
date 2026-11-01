"""API security and the main user path, against an in-process app (demo mode, nothing is scanned)."""
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom.main import create_app, load_i18n

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        c.data_dir = tmp_path
        yield c


def setup_code(c) -> str:
    return (Path(c.data_dir) / "setup-code.txt").read_text().strip()


def create_account(c):
    r = c.post("/api/setup/account", json={"code": setup_code(c), "username": "admin", "password": PW}, headers=HDR)
    assert r.status_code == 200, r.text
    return r


def test_health_is_public(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_everything_else_needs_sign_in(client):
    for path in ("/api/map", "/api/devices", "/api/settings", "/api/integrations", "/api/networks", "/api/scan", "/api/update"):
        assert client.get(path).status_code == 401, path


def test_setup_needs_code_and_only_works_once(client):
    r = client.post("/api/setup/account", json={"code": "WRONG-CODE", "username": "admin", "password": PW}, headers=HDR)
    assert r.status_code == 403
    create_account(client)
    assert not (Path(client.data_dir) / "setup-code.txt").exists()
    r = client.post("/api/setup/account", json={"code": "x", "username": "evil", "password": PW}, headers=HDR)
    assert r.status_code == 409


def test_state_changing_requests_need_header_and_same_origin(client):
    r = client.post("/api/login", json={"username": "a", "password": "b"})
    assert r.status_code == 403 and r.json()["error"] == "auth.err.header"
    r = client.post("/api/login", json={"username": "a", "password": "b"}, headers=dict(HDR, Origin="http://evil.example"))
    assert r.status_code == 403 and r.json()["error"] == "auth.err.origin"


def test_login_rate_limit(client):
    create_account(client)
    client.cookies.clear()
    codes = [client.post("/api/login", json={"username": "admin", "password": "wrong-password"}, headers=HDR).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    # even the right password waits during the lock-out
    assert client.post("/api/login", json={"username": "admin", "password": PW}, headers=HDR).status_code == 429


def test_session_cookie_flags(client):
    r = create_account(client)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_security_headers(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_static_path_traversal_blocked(client):
    for path in ("/../lanloom/main.py", "/%2e%2e/main.py", "/css/../../main.py"):
        r = client.get(path)
        assert r.status_code == 404 or "def create_app" not in r.text


def test_secrets_never_returned_and_stored_encrypted(client):
    create_account(client)
    secret = "super-secret-token-value-123"
    r = client.post("/api/integrations", json={"kind": "hub", "name": "Hub", "config": {"url": "http://192.168.50.14:8123"},
                                               "secrets": {"token": secret}}, headers=HDR)
    assert r.status_code == 200, r.text
    assert secret not in r.text and r.json()["secrets"]["token"] is True
    assert secret not in client.get("/api/integrations").text
    raw = (Path(client.data_dir) / "lanloom.db").read_bytes()
    wal = Path(client.data_dir) / "lanloom.db-wal"
    if wal.exists():
        raw += wal.read_bytes()
    assert secret.encode() not in raw
    key = Path(client.data_dir) / "secret.key"
    assert key.stat().st_mode & 0o077 == 0


def test_demo_wizard_path_builds_a_map(client):
    create_account(client)
    nets = client.get("/api/networks").json()
    assert nets["networks"] and nets["host_mode"]
    r = client.post("/api/scan", json={"cidrs": [nets["networks"][0]["cidr"]]}, headers=HDR)
    assert r.status_code == 200, r.text
    for _ in range(80):
        st = client.get("/api/scan").json()["current"]
        if st["state"] != "running":
            break
        time.sleep(0.25)
    assert st["state"] == "done" and st["found"] > 20
    devices = client.get("/api/devices").json()["devices"]
    kinds = {d["kind"] for d in devices}
    assert {"router", "nas", "printer", "camera", "self", "tv"} <= kinds
    m = client.get("/api/map?view=overview").json()
    assert m["components"] and m["traces"]
    ids = {c["id"] for c in m["components"]}
    assert all(t["a"] in ids and t["b"] in ids for t in m["traces"])
    n = client.get("/api/map?view=network").json()
    assert any(z["id"].startswith("net:") for z in n["zones"])
    detail = client.get("/api/component/" + devices[0]["id"]).json()
    assert detail["component"]["id"] == devices[0]["id"]


def test_deep_scan_needs_consent_outside_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "0")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.setenv("LANLOOM_PROBE", "1")       # no sources, no discovery loops in the test
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        c.data_dir = tmp_path
        create_account(c)
        r = c.post("/api/scan", json={"cidrs": ["192.168.77.0/24"], "deep": True, "consent": False}, headers=HDR)
        assert r.status_code == 400 and r.json()["error"] == "scan.unsafe.consent"
        r = c.post("/api/scan", json={"cidrs": ["203.0.113.0/24"]}, headers=HDR)
        assert r.status_code == 400 and r.json()["error"] == "scan.unsafe.public"


def test_i18n_complete_and_same_keys():
    en, de = load_i18n("en"), load_i18n("de")
    assert en and set(en) == set(de)
    assert all(v.strip() for v in en.values()) and all(v.strip() for v in de.values())
    # placeholders match
    ph = re.compile(r"\{(\w+)\}")
    for k in en:
        assert set(ph.findall(en[k])) == set(ph.findall(de[k])), k


def test_ui_uses_only_known_keys():
    en = load_i18n("en")
    web = Path(__file__).resolve().parent.parent / "lanloom" / "web" / "js"
    missing = set()
    for f in web.glob("*.js"):
        for key in re.findall(r"\bt\('([a-z0-9_.]+)'\s*[,)]", f.read_text(encoding="utf-8")):
            if not key.endswith(".") and key not in en:
                missing.add(key)
    assert not missing, sorted(missing)


def test_update_request_needs_sidecar(client):
    create_account(client)
    r = client.post("/api/update/apply", json={"version": "9.9.9"}, headers=HDR)
    assert r.status_code == 400 and r.json()["error"] == "update.err.no_sidecar"
    (Path(client.data_dir) / "update").mkdir(exist_ok=True)
    (Path(client.data_dir) / "update" / "sidecar.json").write_text(json.dumps({"version": "1", "t": time.time()}))
    r = client.post("/api/update/apply", json={"version": "9.9.9"}, headers=HDR)
    assert r.status_code == 200
    req = json.loads((Path(client.data_dir) / "update" / "request.json").read_text())
    assert req["version"] == "9.9.9" and len(req["nonce"]) == 32
    r = client.post("/api/update/apply", json={"version": "../../evil"}, headers=HDR)
    assert r.status_code == 400
