"""Response headers on every answer, HSTS only over real HTTPS, and the flood guard in front of every
state-changing request (the review of all writing endpoints is in docs/SECURITY-MODEL.md)."""
import pytest
from fastapi.testclient import TestClient

from lanloom import access, auth, flood
from lanloom import config as config_mod
from lanloom.main import CSP, HSTS, SECURITY_HEADERS, STATE_CHANGING, create_app

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
PROXY = "10.9.0.2"


def make(tmp_path, monkeypatch, proxies=""):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.setenv("LANLOOM_TRUSTED_PROXIES", proxies)
    return create_app(config_mod.load())


def peer_app(app, peer):
    async def wrapped(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope, client=(peer, 40000))
        await app(scope, receive, send)
    return wrapped


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with TestClient(make(tmp_path, monkeypatch), base_url="http://lanloom.test") as c:
        c.data_dir = tmp_path
        yield c


def has_all_headers(r):
    for name, value in SECURITY_HEADERS.items():
        assert r.headers.get(name) == value, (r.request.url, name, r.headers.get(name))


def test_every_kind_of_answer_carries_the_headers(client):
    answers = [
        client.get("/"),                                                    # static page
        client.get("/api/health"),                                          # public API
        client.get("/api/state"),                                           # 401 from the guard (early return)
        client.post("/api/login", json={}),                                 # 403: header missing (early return)
        client.post("/api/login", json={}, headers=dict(HDR, Origin="http://evil.test")),   # 403: foreign origin
        client.get("/api/does-not-exist"),                                  # 401/404
        client.get("/nothing-here.js"),                                     # static 404
    ]
    for r in answers:
        has_all_headers(r)
        assert "strict-transport-security" not in r.headers         # plain HTTP never gets HSTS
    assert client.get("/api/health").headers["cache-control"] == "no-store"


def test_csp_closes_the_browser_side():
    parts = dict(p.strip().split(" ", 1) for p in CSP.split(";") if p.strip())
    assert parts["default-src"] == "'self'"
    assert parts["connect-src"] == "'self'"          # the page can talk to its own server only (no telemetry)
    assert parts["script-src"] == "'self'"           # no inline script, no eval, no foreign script
    assert parts["object-src"] == "'none'" and parts["frame-ancestors"] == "'none'" and parts["base-uri"] == "'none'"
    assert "*" not in CSP and "http:" not in CSP and "https:" not in CSP


def test_permissions_policy_denies_device_access():
    pp = SECURITY_HEADERS["Permissions-Policy"]
    for feature in ("camera", "microphone", "geolocation", "usb", "serial", "hid", "midi", "payment"):
        assert "%s=()" % feature in pp
    assert "publickey-credentials" not in pp            # passkeys stay allowed for the page itself
    assert SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"


def test_hsts_only_over_real_https(tmp_path, monkeypatch):
    app = make(tmp_path, monkeypatch, proxies=PROXY)
    with TestClient(app, base_url="https://lanloom.test") as c:           # direct HTTPS
        assert c.get("/api/health").headers["strict-transport-security"] == HSTS
    for peer, expect in ((PROXY, True), ("192.0.2.50", False)):
        with TestClient(peer_app(app, peer), base_url="http://lanloom.test") as c:
            r = c.get("/api/health", headers={"X-Forwarded-Proto": "https"})
            assert ("strict-transport-security" in r.headers) is expect, peer
    assert "includeSubDomains" not in HSTS and "preload" not in HSTS    # LAN names: never pin other hosts


# ------------------------------------------------------------------ flood guard
def test_flood_guard_counts_a_sliding_window():
    g = flood.FloodGuard(3, window=10)
    assert [g.hit("a", now=t) for t in (0, 1, 2)] == [0, 0, 0]
    wait = g.hit("a", now=3)
    assert 6.9 < wait <= 7.0                            # the oldest request leaves the window at t=10
    assert g.hit("b", now=3) == 0                       # other keys are not affected
    assert g.hit("a", now=10.01) == 0


def test_flood_guard_memory_is_bounded():
    g = flood.FloodGuard(5, window=60, max_keys=100)
    for i in range(1000):
        g.hit("k%d" % i, now=i * 0.001)
    assert len(g) <= 100


def test_every_public_write_route_is_capped(tmp_path, monkeypatch):
    """Routes without a session (sign-in, invitation, pairing, restore): each one is behind the per-client cap."""
    monkeypatch.setattr(flood, "PUBLIC_LIMIT", 2)
    routes = sorted((m, p) for (m, p), r in access.RULES.items() if r.role == "public" and m in STATE_CHANGING)
    assert len(routes) >= 10
    with TestClient(make(tmp_path, monkeypatch), base_url="http://lanloom.test") as c:
        for i, (method, path) in enumerate(routes):
            ip = "198.51.100.%d" % (i + 1)
            app = c.app
            with TestClient(peer_app(app, ip), base_url="http://lanloom.test") as one:
                codes = [one.request(method, path, json={}, headers=HDR).status_code for _ in range(3)]
            assert codes[-1] == 429, (method, path, codes)
            assert 429 not in codes[:1], (method, path, codes)


def test_signed_in_writes_are_capped_per_account(tmp_path, monkeypatch):
    monkeypatch.setattr(flood, "USER_LIMIT", 3)
    with TestClient(make(tmp_path, monkeypatch), base_url="http://lanloom.test") as c:
        code = (tmp_path / "setup-code.txt").read_text().strip()
        assert c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200
        codes = [c.post("/api/account/totp/setup", json={}, headers=HDR).status_code for _ in range(4)]
        assert codes[:3] == [200, 200, 200] and codes[3] == 429, codes
        r = c.post("/api/account/totp/setup", json={}, headers=HDR)
        assert r.json()["error"] == "auth.err.rate" and r.json()["wait"] >= 1
        assert c.get("/api/me").status_code != 429          # reading is not capped


def test_open_passkey_ceremonies_are_bounded(tmp_path, monkeypatch):
    app = make(tmp_path, monkeypatch)
    pk = app.state.ll.passkeys
    monkeypatch.setattr(auth.Passkeys, "MAX_PENDING", 50)
    first = pk.login_options("lanloom.test", "http://lanloom.test")["ceremony"]
    for _ in range(200):
        pk.login_options("lanloom.test", "http://lanloom.test")
    assert len(pk._pending) <= 50
    assert first not in pk._pending                      # the oldest went first
