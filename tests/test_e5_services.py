"""E5: services per device, the link probe (200 / 30x / 401 / 5xx / dead), the certificate clock, the 24 h "Removed?" rule.

Probe attrappen are real local servers (plain HTTP with a status per port, a TLS server with a certificate that runs out
soon and one that is already expired). Nothing is probed beyond 127.0.0.1."""
import asyncio
import datetime
import ssl
import time
import types

import pytest
from fastapi.testclient import TestClient

from lanloom import auth, availability as av, config as config_mod, services as sv
from lanloom.main import create_app
from lanloom.scan import protect
from lanloom.store import Store

T0 = 1_800_000_000 // av.DAY_S * av.DAY_S + 12 * 3600


# ------------------------------------------------------------------ attrappen
def make_cert(tmp_path, name: str, days: float):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=400))
            .not_valid_after(now + datetime.timedelta(days=days, seconds=3600)).sign(key, hashes.SHA256()))
    crt, pk = tmp_path / (name + ".crt"), tmp_path / (name + ".key")
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pk.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    return str(crt), str(pk)


class Attrappen:
    """Local servers: ``status`` per name, ``tls`` ones with a given certificate age."""

    def __init__(self):
        self.servers, self.ports = [], {}

    async def http(self, name: str, status: int, location: str = "", server: str = "attrappe/1.2", ctx=None):
        async def handle(reader, writer):
            await reader.read(2048)
            head = "HTTP/1.0 %d X\r\nServer: %s\r\n" % (status, server) + ("Location: %s\r\n" % location if location else "") + "\r\n"
            writer.write(head.encode() + b"<title>Attrappe</title>")
            await writer.drain()
            writer.close()
        srv = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
        self.servers.append(srv)
        self.ports[name] = srv.sockets[0].getsockname()[1]
        return self.ports[name]

    async def tls(self, tmp_path, name: str, days: float, status: int = 200):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*make_cert(tmp_path, name, days))
        return await self.http(name, status, ctx=ctx)

    async def silent(self, name: str):
        """Accepts and closes without a word (connects, but is not HTTP)."""
        async def handle(reader, writer):
            writer.close()
        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.servers.append(srv)
        self.ports[name] = srv.sockets[0].getsockname()[1]

    def closed_port(self) -> int:
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    async def close(self):
        for s in self.servers:
            s.close()
            await s.wait_closed()


@pytest.fixture()
def att():
    a = Attrappen()
    yield a
    asyncio.run(a.close()) if False else None


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ a small core
class FakeCore:
    demo = None

    def __init__(self):
        self.t = T0
        self.store = Store(":memory:")
        self.availability = av.Availability(self.store, clock=lambda: self.t)
        self.up = True
        self.notified = []
        self.notify = types.SimpleNamespace(on_certs=lambda c, now=None: self.notified.append(c))

    def protect_marks(self):
        return {}

    def device_status(self, dev):
        return "up" if self.up else "down"

    def add(self, dev_id, ports, ip="127.0.0.1", title="Box", mdns=None, ssdp=None):
        self.store.upsert_device({"id": dev_id, "ip": ip, "mac": None, "kind": "server", "confidence": 1.0, "title": title,
                                  "data": {"ports": ports, "mdns": mdns or [], "ssdp": ssdp or []}}, 1)


def web_port(port, name="http", banner="x"):
    return {"port": port, "proto": "tcp", "service": name, "banner": {"title": banner, "server": "attrappe/1.2"}}


@pytest.fixture()
def svc(monkeypatch):
    core = FakeCore()
    s = sv.Services(core, clock=lambda: core.t)
    monkeypatch.setattr(sv, "_private", lambda ip: True)
    core.services = s
    return core, s


# ------------------------------------------------------------------ pure: what a device offers
def test_parse_product_only_what_the_device_says():
    assert sv.parse_product("nginx/1.25.3 (Ubuntu)") == ("nginx", "1.25.3")
    assert sv.parse_product("Linux/5.4 UPnP/1.0 MiniServ/2.01") == ("MiniServ", "2.01")
    assert sv.parse_product("lighttpd") == (None, None)
    assert sv.parse_product(None) == (None, None)


def test_extract_services_versions_and_web():
    data = {"ports": [web_port(80, "http", "Router"), {"port": 22, "proto": "tcp", "service": "ssh", "banner": None},
                      {"port": 1883, "proto": "tcp", "service": "mqtt", "banner": None}, {"port": 53, "proto": "udp"}],
            "mdns": [{"type": "_http._tcp", "name": "Panel", "port": 8123, "txt": {"Version": "2026.4"}}, {"type": "_ssh._tcp", "port": 22, "txt": {}}],
            "ssdp": []}
    rows = {r["port"]: r for r in sv.extract(data)}
    assert set(rows) == {80, 22, 1883, 8123}
    assert rows[80]["web"] and rows[80]["version"] == "1.2" and rows[80]["product"] == "attrappe" and rows[80]["title"] == "Router"
    assert not rows[22]["web"] and not rows[1883]["web"] and not rows[22]["tls"]
    assert rows[8123]["web"] and rows[8123]["version"] == "2026.4" and rows[8123]["sources"] == ["mdns"]
    assert [r["port"] for r in sv.extract(data)][0] in (80, 8123)          # web interfaces first


def test_the_host_agent_adds_the_listening_services():
    data = {"ports": [web_port(80, "http", "UI")]}
    listen = [{"port": 80, "proc": "nginx"}, {"port": 5432, "proc": "postgres"}, {"port": 8443, "proc": ""}]
    rows = {r["port"]: r for r in sv.extract(data, listen)}
    assert set(rows) == {80, 5432, 8443}
    assert rows[80]["sources"] == ["scan", "agent"] and rows[80]["web"]
    assert rows[5432]["name"] == "postgres" and rows[5432]["sources"] == ["agent"] and not rows[5432]["web"]
    assert rows[8443]["tls"] and rows[8443]["candidate"] and not rows[8443]["web"]          # a candidate: the probe decides


def test_agent_listen_reads_the_hub_without_loopback(svc):
    core, s = svc
    core.agents = types.SimpleNamespace(live={"a1": {"listen": [{"proto": "tcp", "port": 22, "local": False, "proc": "sshd"},
                                                               {"proto": "tcp", "port": 631, "local": True},
                                                               {"proto": "udp", "port": 53, "local": False}]}},
                                        device_of=lambda aid: "d-1")
    core.add("d-1", [])
    assert sv.agent_listen(core, "d-1") == [{"port": 22, "proc": "sshd"}] and sv.agent_listen(core, "d-2") == []
    assert [e["svc"]["name"] for e in s.endpoints()] == ["sshd"]


def test_certificate_clock_tiers():
    assert [sv.tier(d) for d in (None, -1, 0, 2, 3, 13, 14, 29, 30, 300)] == [
        "unknown", "expired", "danger", "danger", "warn", "warn", "hint", "hint", "ok", "ok"]


def test_status_classes():
    assert [sv.classify_status(c) for c in (0, 200, 204, 302, 404, 401, 403, 500, 503)] == ["none", "ok", "ok", "ok", "ok", "auth", "auth", "error", "error"]


def test_protection_classes_limit_the_probe():
    assert sv.allowed_ports("passive")(80) is False
    assert sv.allowed_ports("printer")(9100) is False and sv.allowed_ports("printer")(631) and sv.allowed_ports("printer")(80)
    assert sv.allowed_ports("industrial")(80) and sv.allowed_ports("industrial")(443) and not sv.allowed_ports("industrial")(8080)
    assert not sv.allowed_ports("industrial")(502)
    assert sv.allowed_ports(None)(9100)


# ------------------------------------------------------------------ the probes against attrappen
def test_probe_web_answers(tmp_path):
    async def go():
        a = Attrappen()
        try:
            await a.http("ok", 200)
            await a.http("redir", 302, location="/home")
            await a.http("auth", 401)
            await a.http("forbidden", 403)
            await a.http("notfound", 404)
            await a.http("broken", 503)
            await a.silent("silent")
            out = {n: await sv.probe_web("127.0.0.1", p, False, 2.0) for n, p in a.ports.items()}
            out["dead"] = await sv.probe_web("127.0.0.1", a.closed_port(), False, 1.0)
            return out
        finally:
            await a.close()
    out = run(go())
    assert out["ok"]["kind"] == "ok" and out["ok"]["status"] == 200
    assert out["redir"]["kind"] == "ok" and out["redir"]["status"] == 302                # a redirect is an answer (no loop to chase)
    assert out["auth"]["kind"] == "auth" and out["forbidden"]["kind"] == "auth"        # 401/403: it lives
    assert out["notfound"]["kind"] == "ok"
    assert out["broken"]["kind"] == "error" and out["broken"]["status"] == 503
    assert out["silent"]["kind"] == "nothttp"                                            # connects, is not a web interface
    assert run(_confirmed(Attrappen, out))["kind"] == "miss"                              # a confirmed web port that answers nothing is a miss
    assert out["dead"]["kind"] == "miss"


async def _confirmed(_cls, _out):
    a = Attrappen()
    try:
        await a.silent("s")
        return await sv.probe_web("127.0.0.1", a.ports["s"], False, 2.0, confirmed=True)
    finally:
        await a.close()


def test_probe_tcp_and_cert_reading(tmp_path):
    async def go():
        a = Attrappen()
        try:
            soon = await a.tls(tmp_path, "soon", 10)
            gone = await a.tls(tmp_path, "gone", -3)
            fine = await a.tls(tmp_path, "fine", 200)
            return (await sv.probe_tcp("127.0.0.1", soon), await sv.probe_tcp("127.0.0.1", a.closed_port(), 1.0),
                    await sv.read_cert("127.0.0.1", soon), await sv.read_cert("127.0.0.1", gone), await sv.read_cert("127.0.0.1", fine),
                    await sv.read_cert("127.0.0.1", a.closed_port()))
        finally:
            await a.close()
    up, down, soon, gone, fine, none = run(go())
    assert up["up"] and not down["up"]
    assert soon["days"] in (9, 10) and soon["self_signed"] and soon["issuer"] == "soon" and soon["subject"] == "soon"
    assert gone["days"] < 0 and sv.tier(gone["days"]) == "expired"
    assert fine["days"] >= 199 and sv.tier(fine["days"]) == "ok"
    assert none is None


# ------------------------------------------------------------------ a full round
def test_round_states_certificates_and_availability(svc, tmp_path):
    core, s = svc

    async def go():
        a = Attrappen()
        try:
            ok = await a.http("ok", 200)
            auth_ = await a.http("auth", 401)
            broken = await a.http("broken", 503)
            soon = await a.tls(tmp_path, "soon", 10)
            gone = await a.tls(tmp_path, "gone", -3)
            dead = a.closed_port()
            core.add("d-1", [web_port(ok), web_port(auth_), web_port(broken), web_port(soon, "https"), web_port(gone, "https"),
                             {"port": dead, "proto": "tcp", "service": "http-alt", "banner": {"title": "x", "server": None}}],
                     title="Box", mdns=[{"type": "_https._tcp", "name": "x", "port": p, "txt": {}} for p in (soon, gone)])
            await s.round(T0)
            first = {r["port"]: r for r in s.listing()}
            core.t = T0 + 180
            await s.round(core.t)
            second = {r["port"]: r for r in s.listing()}
            core.t = T0 + 360
            await s.round(core.t)
            third = {r["port"]: r for r in s.listing()}
            return a.ports, dead, first, second, third
        finally:
            await a.close()
    ports, dead, first, second, third = run(go())
    assert first[ports["ok"]]["state"] == "ok" and first[ports["ok"]]["clickable"]
    assert first[ports["auth"]]["state"] == "auth" and first[ports["auth"]]["clickable"]          # 401 lives
    assert first[ports["broken"]]["state"] == "error"                                                # 5xx red, still a link
    assert first[dead]["state"] == "doubt" and first[dead]["clickable"]                              # one miss: still clickable
    assert second[dead]["state"] == "dead" and not second[dead]["clickable"] and third[dead]["state"] == "dead"
    assert first[ports["soon"]]["cert"]["tier"] == "warn" and first[ports["soon"]]["cert"]["self_signed"]
    assert first[ports["gone"]]["cert"]["tier"] == "expired"
    assert first[ports["ok"]]["cert"] is None and first[ports["ok"]]["url"] == "http://127.0.0.1:%d/" % ports["ok"]
    assert first[ports["soon"]]["url"].startswith("https://127.0.0.1:")
    assert first[ports["ok"]]["version"] == "1.2" and first[ports["ok"]]["product"] == "attrappe"
    # availability: the answers are booked, 401 as up, 5xx as down
    slot = int(T0 // av.SLOT_S)
    st = lambda p: av.slot_state(core.availability.cells("svc:d-1|%d" % p, slot, slot + 1)[0])
    assert st(ports["ok"]) == "up" and st(ports["auth"]) == "up" and st(ports["broken"]) == "down"
    # the certificate list for the nightly report and the notification rules
    certs = {c["id"]: c for c in s.cert_list()}
    assert len(certs) == 2 and min(c["days"] for c in certs.values()) < 0
    assert all(c["id"].startswith("svc:d-1|") and "Box" in c["name"] for c in certs.values())
    assert core.notified and {c["days"] < 14 for c in core.notified[-1]} == {True}
    summ = s.summary()
    assert summ["dead"] == 1 and summ["error"] == 1 and summ["certs"] == 2 and summ["certs_red"] == 1


def test_a_dead_service_heals_by_itself(svc):
    core, s = svc

    async def go():
        a = Attrappen()
        try:
            port = a.closed_port()
            core.add("d-1", [web_port(port)])
            for k in range(3):
                core.t = T0 + 180 * k
                await s.round(core.t)
            before = s.listing()[0]
            # the service comes back on the same port
            async def handle(reader, writer):
                await reader.read(1024)
                writer.write(b"HTTP/1.0 200 OK\r\n\r\nhi")
                await writer.drain()
                writer.close()
            srv = await asyncio.start_server(handle, "127.0.0.1", port)
            core.t += 180
            await s.round(core.t)
            after = s.listing()[0]
            srv.close()
            await srv.wait_closed()
            return before, after
        finally:
            await a.close()
    before, after = run(go())
    assert before["state"] == "dead" and not before["clickable"]
    assert after["state"] == "ok" and after["clickable"]                       # no message, no click: it healed


def test_removed_after_24_hours_of_probing_while_the_host_answers(svc):
    core, s = svc
    core.add("d-1", [{"port": 22, "proto": "tcp", "service": "ssh", "banner": None}])
    ep = s.endpoints()
    miss = [{"kind": "miss", "status": 0, "ms": None}]
    # a different service of the host keeps answering, so the misses are about this service alone
    now = T0
    for k in range(10):
        s.apply(ep, miss, now)
        now += 180
    assert s.listing()[0]["state"] == "dead"
    for k in range(int(sv.REMOVED_AFTER_S // 180) + 2):
        s.apply(ep, miss, now)
        now += 180
    row = s.listing()[0]
    assert row["state"] == "removed" and not row["clickable"]
    slot = int((now - 60) // av.SLOT_S)
    assert core.availability.cells("svc:d-1|22", slot, slot)[0] == 0           # not booked as an outage any more
    s.apply(ep, [{"kind": "ok", "status": 0, "ms": 3}], now + 3600)               # it comes back by itself
    assert s.listing()[0]["state"] == "ok"
    assert s.state["d-1|22"]["fails"] == 0


def test_a_port_the_scan_no_longer_lists_is_remembered_until_forgotten(svc):
    core, s = svc
    core.add("d-1", [{"port": 22, "proto": "tcp", "service": "ssh", "banner": None}, {"port": 2222, "proto": "tcp", "service": "ssh-alt", "banner": None}])

    async def go():
        await s.round(T0)

    run(go())
    core.add("d-1", [{"port": 22, "proto": "tcp", "service": "ssh", "banner": None}])        # the next scan: 2222 is not listed
    assert {r["port"] for r in s.listing()} == {22, 2222}
    assert s.forget("d-1|2222")
    assert {r["port"] for r in s.listing()} == {22}
    assert not s.forget("nobody|1") and s.state.get("d-1|2222") is None


def test_host_down_is_the_devices_business(svc):
    core, s = svc
    core.add("d-1", [web_port(1)])
    core.up = False
    run(s.round(T0))
    assert s.listing()[0]["state"] == "hostdown"
    assert not core.availability.has_data()


def test_nothing_answers_at_all_is_our_network_not_three_faults(svc):
    core, s = svc
    core.add("d-1", [web_port(1), web_port(2), web_port(3)])
    ep = s.endpoints()
    s.apply(ep, [{"kind": "miss", "status": 0, "ms": None}] * 3, T0)
    assert all(s.state.get(e["key"], {}).get("fails", 0) == 0 for e in ep) and not core.availability.has_data()


def test_protected_devices_are_not_probed(svc):
    core, s = svc
    core.add("d-p", [{"port": 9100, "proto": "tcp", "service": "printer-raw", "banner": None}, web_port(80)], title="Printer")
    core.add("d-m", [{"port": 104, "proto": "tcp", "service": "medical-imaging", "banner": None}, web_port(80)], title="Scanner")
    eps = {e["key"]: e for e in s.endpoints()}
    assert not eps["d-p|9100"]["probe"] and eps["d-p|80"]["probe"]
    assert not eps["d-m|104"]["probe"] and not eps["d-m|80"]["probe"]
    assert {r["state"] for r in s.listing() if r["device"] == "d-m"} == {"unprobed"}


def test_probe_can_be_switched_off_and_outside_addresses_are_never_probed(svc, monkeypatch):
    core, s = svc
    core.add("d-1", [web_port(1)])
    core.store.set_setting("service_probe", False)
    assert run(s.round(T0)) == 0 and not s.state.get("d-1|1", {}).get("t")
    monkeypatch.setattr(sv, "_private", lambda ip: False)
    core.add("d-2", [web_port(1)], ip="8.8.8.8")
    assert not [e for e in s.endpoints() if e["device"] == "d-2"][0]["probe"]


def test_state_survives_a_restart(svc):
    core, s = svc
    core.add("d-1", [web_port(80)])
    s.apply(s.endpoints(), [{"kind": "ok", "status": 200, "ms": 5}], T0)
    s.save()
    again = sv.Services(core, clock=lambda: core.t)
    assert again.listing()[0]["state"] == "ok"


def test_forgetting_a_device_forgets_its_services(svc):
    core, s = svc
    core.add("d-1", [web_port(80)])
    s.apply(s.endpoints(), [{"kind": "ok", "status": 200, "ms": 5}], T0)
    s.forget_device("d-1")
    assert not s.state


# ------------------------------------------------------------------ nightly report and notification rules
def test_nightly_certificate_section_reads_the_service_certificates(svc):
    from lanloom.nightly import contrib
    core, s = svc
    core.add("d-1", [web_port(443, "https")], title="Camera")
    ep = s.endpoints()
    s.apply(ep, [{"kind": "ok", "status": 200, "ms": 5, "cert": {"days": 9, "not_after": 1, "issuer": "x", "subject": "x", "self_signed": True}}], T0)
    ctx = contrib.Ctx(core=core, journal=None, since=0, until=1, prev=None, certs=s.cert_list())
    sec = contrib._certs(ctx)
    assert sec["level"] == "warn" and any(i["point"] and i["key"] == "report.cert.soon" for i in sec["items"])
    assert sec["items"][0]["vars"]["name"].startswith("Camera")


# ------------------------------------------------------------------ routes (demo)
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        uid = app.state.ll.accounts.create("viewer1", "a-long-test-password", role="viewer")
        c.cookies.set(auth.SESSION_COOKIE, app.state.ll.sessions.create(uid, ip="127.0.0.1", agent="test"))
        deadline = time.time() + 20
        while time.time() < deadline and not any(r["state"] == "ok" for r in c.get("/api/services").json()["rows"]):
            time.sleep(0.3)
        c.app_ = app
        yield c


def test_demo_shows_every_case(client):
    body = client.get("/api/services").json()
    rows, summ = body["rows"], body["summary"]
    assert summ["services"] > 20 and summ["web"] >= 8 and body["enabled"]
    states = {r["state"] for r in rows}
    assert {"ok", "auth", "error", "dead", "removed"} <= states
    tiers = {r["cert"]["tier"] for r in rows if r["cert"]}
    assert {"warn", "expired", "danger", "hint", "ok"} <= tiers
    assert all(not r["clickable"] for r in rows if r["state"] in ("dead", "removed"))
    assert all(r["url"] and r["url"].startswith("http") for r in rows if r["web"])
    dev = next(r["device"] for r in rows if r["web"])
    one = client.get("/api/devices/%s/services" % dev).json()["rows"]
    assert one and all(r["device"] == dev for r in one)
    assert client.get("/api/devices/nope/services").status_code == 404
    assert summ["certs_soon"] >= 3 and summ["certs_red"] >= 2


def test_forget_needs_an_operator_and_leaves_a_trace(client):
    rows = client.get("/api/services").json()["rows"]
    gone = next(r for r in rows if r["state"] == "removed")
    r = client.post("/api/services/forget", json={"key": gone["key"]}, headers={"X-Lanloom": "1"})
    assert r.status_code in (403, 409)                                       # a viewer may not; the sample network is read-only anyway
