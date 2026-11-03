"""Sign-in rate limit behind a reverse proxy: whose address counts, IPv6 grouping, bounded memory."""
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from lanloom import auth
from lanloom import config as config_mod
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}
PROXY = "10.9.0.2"


def addr(peer, proxies="", **headers):
    return auth.client_address(peer, Headers(headers=headers), auth.parse_proxies(proxies))


# ------------------------------------------------------------------ whose address counts
def test_without_trusted_proxies_only_the_peer_counts():
    assert addr("192.0.2.7", x_forwarded_for="198.51.100.1") == "192.0.2.7"
    assert addr("192.0.2.7", x_real_ip="198.51.100.1") == "192.0.2.7"


def test_headers_from_an_untrusted_peer_are_ignored():
    assert addr("192.0.2.7", "10.9.0.0/24", **{"x-forwarded-for": "198.51.100.1"}) == "192.0.2.7"


def test_trusted_proxy_forwards_the_client():
    assert addr(PROXY, "10.9.0.0/24", **{"x-forwarded-for": "198.51.100.1"}) == "198.51.100.1"
    assert addr(PROXY, "10.9.0.2", **{"x-real-ip": "198.51.100.1"}) == "198.51.100.1"


def test_right_most_untrusted_entry_wins_against_forged_entries():
    # the client sent "1.1.1.1" itself; the trusted proxy appended the address it really saw
    headers = {"x-forwarded-for": "1.1.1.1, 198.51.100.9"}
    assert addr(PROXY, "10.9.0.0/24", **headers) == "198.51.100.9"
    # a chain of two trusted proxies is skipped from the right
    headers = {"x-forwarded-for": "1.1.1.1, 198.51.100.9, 10.9.0.3"}
    assert addr(PROXY, "10.9.0.0/24", **headers) == "198.51.100.9"


def test_x_forwarded_for_beats_x_real_ip_and_garbage_falls_back():
    assert addr(PROXY, PROXY, **{"x-forwarded-for": "198.51.100.1", "x-real-ip": "203.0.113.5"}) == "198.51.100.1"
    assert addr(PROXY, PROXY, **{"x-forwarded-for": "not-an-ip"}) == PROXY
    assert addr(PROXY, PROXY, **{"x-real-ip": "nonsense"}) == PROXY
    assert addr(PROXY, PROXY, **{"x-forwarded-for": "198.51.100.1:4444"}) == "198.51.100.1"
    assert addr(PROXY, PROXY, **{"x-forwarded-for": "[2001:db8::1]:4444"}) == "2001:db8::1"


def test_parse_proxies_ignores_invalid_entries():
    nets = auth.parse_proxies(" 10.0.0.1 , fd00::/8, nonsense, ,192.168.1.0/24")
    assert [str(n) for n in nets] == ["10.0.0.1/32", "fd00::/8", "192.168.1.0/24"]
    assert auth.parse_proxies("") == ()


def test_config_reads_trusted_proxies(monkeypatch):
    monkeypatch.setenv("LANLOOM_TRUSTED_PROXIES", "10.0.0.1, fd00::/8")
    assert config_mod.load().trusted_proxies == ("10.0.0.1", "fd00::/8")
    monkeypatch.delenv("LANLOOM_TRUSTED_PROXIES")
    assert config_mod.load().trusted_proxies == ()


# ------------------------------------------------------------------ grouping
def test_ipv6_counts_per_64_and_ipv4_per_address():
    assert auth.limit_key("2001:db8:1:2:aaaa::1") == auth.limit_key("2001:db8:1:2:bbbb::2") == "2001:db8:1:2::/64"
    assert auth.limit_key("2001:db8:1:3::1") != auth.limit_key("2001:db8:1:2::1")
    assert auth.limit_key("192.0.2.1") == "192.0.2.1"
    assert auth.limit_key("::ffff:192.0.2.1") == "192.0.2.1"
    assert auth.limit_key("testclient") == "testclient"


# ------------------------------------------------------------------ bounded memory
class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def lock_out(limiter, client):
    for _ in range(5):
        limiter.failure(client)
    assert limiter.wait_seconds(client) > 0


def test_flooding_the_limiter_does_not_unlock_an_attacker(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(auth.time, "time", clock)
    limiter = auth.LoginLimiter(max_entries=100)
    lock_out(limiter, "attacker")
    for i in range(500):             # many addresses with one failure each
        clock.t += 0.01
        limiter.failure("flood-%d" % i)
    assert limiter.wait_seconds("attacker") > 0
    assert len(limiter._state) <= 100


def test_expired_entries_are_dropped_first(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(auth.time, "time", clock)
    limiter = auth.LoginLimiter(max_entries=10)
    for i in range(10):
        limiter.failure("old-%d" % i)
    lock_out(limiter, "attacker")
    clock.t += 4000                  # past every lock-out
    lock_out(limiter, "fresh")
    assert "fresh" in limiter._state and limiter.wait_seconds("fresh") > 0
    assert len(limiter._state) <= 10


# ------------------------------------------------------------------ app behind a proxy
def peer_app(app, peer):
    async def wrapped(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope, client=(peer, 40000))
        await app(scope, receive, send)
    return wrapped


@pytest.fixture()
def proxied(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.setenv("LANLOOM_TRUSTED_PROXIES", PROXY)
    app = create_app(config_mod.load())
    with TestClient(peer_app(app, PROXY), base_url="http://lanloom.test") as c:
        yield c


def login(c, client_ip, password="wrong-password-123"):
    return c.post("/api/login", json={"username": "nobody", "password": password},
                  headers=dict(HDR, **{"X-Forwarded-For": client_ip}))


def test_one_client_behind_the_proxy_cannot_lock_out_everybody(proxied):
    for _ in range(5):
        assert login(proxied, "198.51.100.1").status_code == 401
    assert login(proxied, "198.51.100.1").status_code == 429
    assert login(proxied, "198.51.100.2").status_code == 401     # another person is not locked out


def test_forwarded_proto_only_from_a_trusted_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.setenv("LANLOOM_TRUSTED_PROXIES", PROXY)
    for peer, secure in ((PROXY, True), ("192.0.2.50", False)):
        app = create_app(config_mod.load())
        with TestClient(peer_app(app, peer), base_url="http://lanloom.test") as c:
            code = (tmp_path / "setup-code.txt").read_text().strip()
            r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"},
                       headers=dict(HDR, **{"X-Forwarded-Proto": "https"}))
            assert r.status_code == 200, r.text
            cookie = r.headers["set-cookie"].lower()
            assert ("; secure" in cookie) is secure, (peer, cookie)
        (tmp_path / "lanloom.db").unlink()
        for side in tmp_path.glob("lanloom.db-*"):
            side.unlink()
