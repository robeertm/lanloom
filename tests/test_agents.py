"""Host agent and container source (E4): secret filter, read-only clients, pairing and tokens, buffer, noise filter,
conversations, the Running view and the agent's own report.

The container runtime is a stand-in served on a Unix socket in the test's directory; it records every request so the
read-only rule (GET only) is checked against what really went over the wire. Its answers carry known secrets in the
environment, the command line, labels and mounts - none of them may reach a report, the database or an answer.
"""
import http.server
import json
import os
import re
import socket
import socketserver
import struct
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import __version__, agents as agents_mod, auth
from lanloom import config as config_mod
from lanloom.agent import PROTO, containers, dbus, overlay, scrub, sockets, system, transport
from lanloom.agent import runner
from lanloom.main import create_app

ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = ROOT / "lanloom" / "agent"
HDR = {"X-Lanloom": "1"}
# assembled at run time: the fakes must not sit in the repository as literals (tests/test_fixtures_secrets.py)
SECRETS = ("hunter2-secret-value", "s3cr3t-db-password", "/srv/very/secret/mount", "AK" + "IA1234567890ABCDEF12",
           "tok_" + "9f8e7d6c5b4a" + "39281706f5e4d3c2b1a0")


# ---------------------------------------------------------------------- a container runtime stand-in
def _container_rows():
    return [
        {"Id": "a" * 64, "Names": ["/web"], "Image": "example/web:1.2", "State": "running", "Status": "Up 3 hours (healthy)",
         "Created": int(time.time()) - 86400, "Command": "serve --password " + SECRETS[0],
         "Labels": {"com.docker.compose.project": "site", "com.docker.compose.service": "web", "api_token": SECRETS[4]},
         "Mounts": [{"Source": SECRETS[2]}], "Ports": [{"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}],
         "NetworkSettings": {"Networks": {"site_default": {"IPAddress": "172.20.0.2"}}}, "HostConfig": {"NetworkMode": "site_default"}},
        {"Id": "b" * 64, "Names": ["/job"], "Image": "example/job:1", "State": "exited", "Status": "Exited (0) 1 minute ago",
         "Created": int(time.time()) - 120, "Command": "run", "Labels": {}, "HostConfig": {"NetworkMode": "host"}},
    ]


def _inspect(cid):
    return {"Id": cid, "State": {"Status": "running", "Pid": 0, "ExitCode": 0, "StartedAt": "2026-11-17T10:00:00.123456789Z",
                                 "FinishedAt": "0001-01-01T00:00:00Z", "Health": {"Status": "healthy", "Log": [{"Output": SECRETS[1]}]}},
            "Config": {"Env": ["DB_PASSWORD=" + SECRETS[1], "AWS_KEY=" + SECRETS[3]], "Cmd": ["--password", SECRETS[0]]},
            "RestartCount": 2, "Mounts": [{"Source": SECRETS[2]}]}


class _Runtime(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, status=200):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.server.seen.append(("GET", self.path))
        if self.path == "/version":
            return self._send({"Version": "27.3.1", "ApiVersion": "1.47", "Components": [{"Name": "Engine"}]})
        if self.path.startswith("/containers/json"):
            return self._send(_container_rows())
        m = re.match(r"/containers/([0-9a-f]{12})/json", self.path)
        if m:
            return self._send(_inspect(m.group(1)))
        if "/stats" in self.path:
            return self._send({"networks": {"eth0": {"rx_bytes": 1000, "tx_bytes": 2000}}, "Env": SECRETS[1]})
        self._send({"message": "no"}, 404)

    def _refuse(self):
        self.server.seen.append((self.command, self.path))
        self._send({"message": "read only"}, 405)

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse


@pytest.fixture()
def runtime_sock(tmp_path):
    path = str(tmp_path / "rt.sock")
    srv = _Runtime(path, _Handler)
    srv.seen = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield path, srv
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------------- secret filter
def test_container_row_keeps_only_the_positive_list():
    item = containers.parse_list_row(_container_rows()[0])
    flat = json.dumps(item)
    for s in SECRETS:
        assert s not in flat
    assert item["name"] == "web" and item["health"] == "healthy" and item["project"] == "site" and item["service"] == "web"
    assert item["ports"] == [{"target": 80, "port": 8080, "proto": "tcp"}]
    assert scrub.check(item) == []
    assert "api_token" not in flat and "Command" not in flat


def test_text_filter_hides_values_of_secret_keys_and_tokens():
    assert scrub.text("db password=abc123") == "db password=[hidden]"
    assert scrub.text("url http://user:pw@host/x").count("[hidden]") == 1  # secret-scan: fake
    assert "9f8e7d6c5b4a" not in scrub.text("key " + SECRETS[4])
    assert scrub.text("kitchen_light_2024_ceiling_lamp") == "kitchen_light_2024_ceiling_lamp"
    assert len(scrub.text("x" * 500)) == scrub.MAX_TEXT


def test_check_and_strip_find_forbidden_keys_anywhere():
    rep = {"containers": {"items": [{"name": "a", "Env": ["A=1"], "labels": {"x": 1}}]}, "units": {"failed": [{"description": "d"}]}}
    bad = scrub.check(rep)
    assert "/containers/items[0]/Env" in bad and "/containers/items[0]/labels" in bad and "/units/failed[0]/description" in bad
    clean = scrub.strip(rep)
    assert scrub.check(clean) == []
    assert clean["containers"]["items"][0] == {"name": "a"}


# ---------------------------------------------------------------------- read-only clients (static and on the wire)
def test_runtime_client_sends_get_only_static():
    for name in ("containers.py", "overlay.py"):
        src = (AGENT_DIR / name).read_text(encoding="utf-8")
        assert not re.search(r"""["'](POST|PUT|DELETE|PATCH|HEAD|CONNECT)["']""", src), name
        assert len(re.findall(r"\.request\(", src)) == 1, name          # one request call, with a literal "GET"
        assert re.search(r"""\.request\("GET", """, src), name


def test_bus_client_can_only_call_listing_and_reading_methods():
    assert dbus.ALLOWED == {("org.freedesktop.DBus", "Hello"), ("org.freedesktop.systemd1.Manager", "ListUnits"),
                            ("org.freedesktop.DBus.Properties", "Get")}
    src = (AGENT_DIR / "dbus.py").read_text(encoding="utf-8")
    for word in ("StartUnit", "StopUnit", "RestartUnit", "KillUnit", "ResetFailed", "Reload", "EnableUnit", "SetProperties"):
        assert word not in src
    with pytest.raises(dbus.BusError):
        dbus.Bus.call(object.__new__(dbus.Bus), "x", "/", "org.freedesktop.systemd1.Manager", "StartUnit")


def test_bus_marshalling_round_trip():
    buf = bytearray()
    rows = [["a.service", "A", "loaded", "failed", "failed", "", "/org/a", 0, "", "/"]]
    dbus._put(buf, "a(ssssssouso)", rows)
    assert dbus._Reader(bytes(buf)).get("a(ssssssouso)") == rows
    buf = bytearray()
    dbus._put(buf, "v", ("t", 1_700_000_000_000_000))
    assert dbus._Reader(bytes(buf)).get("v") == 1_700_000_000_000_000
    assert dbus._usec(1_700_000_000_123_456) == 1700000000.1 and dbus._usec(0) is None


def test_runtime_stand_in_read_only_and_no_secret(runtime_sock):
    path, srv = runtime_sock
    part, rt = containers.collect("unix://" + path)
    assert part["available"] and part["runtime"] == "docker" and len(part["items"]) == 2
    assert {m for m, _ in srv.seen} == {"GET"}
    flat = json.dumps(part)
    for s in SECRETS:
        assert s not in flat
    web = next(c for c in part["items"] if c["name"] == "web")
    assert web["restarts"] == 2 and web["started"] and web["tx"] == 2000 and web["rx"] == 1000
    job = next(c for c in part["items"] if c["name"] == "job")
    assert "tx" not in job                                              # host network and stopped: no counters of its own
    n = len(srv.seen)
    containers.collect("unix://" + path, rt)                            # inspect answers are reused while the state holds
    assert sum(1 for _, p in srv.seen[n:] if p.endswith("/json") and "/containers/" in p and "json?" not in p) == 0


def test_runtime_url_forms():
    assert containers.normalise_url("/var/run/x.sock") == "unix:///var/run/x.sock"
    assert containers.normalise_url("tcp://192.0.2.5:2375") == "http://192.0.2.5:2375"
    assert containers.normalise_url("http://user:pw@192.0.2.5:2375") == ""  # secret-scan: fake
    assert containers.normalise_url("ftp://x") == ""
    assert containers.normalise_url("unix:///a/../b") == ""


def test_overlay_status_keeps_names_addresses_online_only():
    st = {"Self": {"HostName": "me", "TailscaleIPs": ["192.0.2.1"]},  # identifier: field names of the status answer
          "Peer": {"k1": {"HostName": "laptop", "TailscaleIPs": ["192.0.2.2"], "Online": True, "LastSeen": "2026-11-17T10:00:00Z",  # identifier
                          "OS": "os-a", "PublicKey": "nodekey:abc", "UserID": 5, "Addrs": ["203.0.113.9:41641"]}}}
    out = overlay.parse(st)
    assert out["peers"] == [{"name": "laptop", "ips": ["192.0.2.2"], "online": True, "last_seen": out["peers"][0]["last_seen"], "os": "os-a"}]
    assert "nodekey" not in json.dumps(out) and "203.0.113.9" not in json.dumps(out)


# ---------------------------------------------------------------------- the agent's own collectors
def test_listening_table_parsing():
    text = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 0100007F:0277 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1111 1\n"
            "   1: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2222 1\n"
            "   2: 0201A8C0:9C40 0101A8C0:01BB 01 00000000:00000000 00:00000000 00000000     0        0 3333 1\n")
    rows = sockets._parse_table(text, False, "tcp")
    assert [(r["ip"], r["port"]) for r in rows] == [("127.0.0.1", 631), ("0.0.0.0", 22)]


def test_flow_meter_sums_per_conversation():
    m = sockets.FlowMeter()
    m.listen_ports = {22}
    m._add({"lip": "192.168.50.12", "lport": 22, "rip": "192.168.50.20", "rport": 50000}, 10, 20)
    m._add({"lip": "192.168.50.12", "lport": 22, "rip": "192.168.50.20", "rport": 50001}, 5, 5)
    m._add({"lip": "192.168.50.12", "lport": 40000, "rip": "203.0.113.20", "rport": 443}, 1, 2)
    m._add({"lip": "127.0.0.1", "lport": 1, "rip": "127.0.0.1", "rport": 2}, 99, 99)
    rows = m.take()
    assert rows[0] == {"lip": "192.168.50.12", "rip": "192.168.50.20", "port": 22, "dir": "in", "tx": 15, "rx": 25}
    assert rows[1]["port"] == 443 and rows[1]["dir"] == "out" and len(rows) == 2
    assert m.take() == []


def test_agent_report_once_has_no_forbidden_field(monkeypatch, runtime_sock):
    """The agent's real collectors on this machine (whatever it offers) plus the runtime stand-in."""
    path, _ = runtime_sock
    monkeypatch.setenv("LANLOOM_AGENT_CONTAINERS", "unix://" + path)
    monkeypatch.setenv("LANLOOM_AGENT_DBUS", str(Path(path).parent / "no-bus"))
    col = runner.Collector()
    col.poll()
    rep = col.report(full=True)
    assert scrub.check(rep) == []
    flat = json.dumps(rep)
    for s in SECRETS:
        assert s not in flat
    for k, v in os.environ.items():                        # no environment value of the agent itself either
        if len(v) >= 12 and k not in ("HOME", "PATH", "PWD", "LANLOOM_AGENT_CONTAINERS", "LANLOOM_AGENT_DBUS") and "/" not in v:
            assert v not in flat, k
    assert rep["units"] == {"available": False, "reason": "no_bus"}
    assert rep["containers"]["available"] and rep["host"]["name"]
    assert set(rep) >= {"t", "flows", "host", "system", "listen", "units", "overlay", "containers"}


def test_agent_selftest():
    from lanloom.agent.__main__ import selftest
    assert selftest() == 0


def test_system_values_from_kernel_files(tmp_path):
    (tmp_path / "loadavg").write_text("0.52 0.40 0.30 1/200 999\n")
    (tmp_path / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    assert system.load(str(tmp_path)) == [0.52, 0.4, 0.3]
    assert system.meminfo(str(tmp_path))["mem_avail"] == 400 * 1024


# ---------------------------------------------------------------------- transport: pairing, buffer, revoke
class _Lanloom(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.server.down:                     # Lanloom away: the connection drops without an answer
            self.close_connection = True
            return
        self.server.calls.append((self.path, self.headers.get("Authorization"), body))
        status, answer = self.server.answer(self.path, body)
        raw = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture()
def lanloom_stub():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Lanloom)
    srv.calls = []
    srv.down = False
    srv.answer = lambda path, body: (200, {"ok": True, "server_version": __version__})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_link_pairs_keeps_token_private_and_buffers_when_offline(tmp_path, lanloom_stub):
    url = "http://127.0.0.1:%d" % lanloom_stub.server_address[1]
    lanloom_stub.answer = lambda path, body: (200, {"agent_id": "ag-1", "token": "T" * 43, "server_version": __version__}) \
        if path == "/api/agent/pair" else (200, {"ok": True, "server_version": __version__})
    link = transport.Link(url, str(tmp_path / "state"), __version__)
    link.pair("ABCD-EFGH", {"name": "h"}, PROTO)
    tok = tmp_path / "state" / "token.json"
    assert oct(tok.stat().st_mode & 0o777) == "0o600" and json.loads(tok.read_text())["token"] == "T" * 43
    assert link.used_code("abcd efgh") and not link.used_code("ZZZZ-ZZZZ")
    link.queue({"t": 1, "flows": []})
    assert link.flush(PROTO) == "ok"
    path, authz, body = lanloom_stub.calls[-1]
    assert path == "/api/agent/report" and authz == "Bearer " + "T" * 43 and body["proto"] == PROTO
    # offline: reports wait (and survive a restart), then go out oldest first
    lanloom_stub.down = True
    for i in range(transport.BUFFER_KEEP_FULL + 5):
        link.queue({"t": i, "flows": [], "listen": [1], "containers": {"available": True, "items": [{"id": "x", "name": "n", "image": "i"}]}})
    assert link.flush(PROTO) == "offline"
    link.save_buffer(force=True)
    lanloom_stub.down = False
    again = transport.Link(url, str(tmp_path / "state"), __version__)
    assert len(again.buffer) == transport.BUFFER_KEEP_FULL + 5
    assert "listen" not in again.buffer[0] and again.buffer[0]["containers"]["bytes_only"]
    assert "listen" in again.buffer[-1]
    assert again.flush(PROTO) == "ok" and not again.buffer
    ts = [r["t"] for _, _, b in lanloom_stub.calls if b.get("reports") for r in b["reports"]]
    assert ts[-(transport.BUFFER_KEEP_FULL + 5):] == list(range(transport.BUFFER_KEEP_FULL + 5))


def test_link_stops_when_revoked(tmp_path, lanloom_stub, monkeypatch):
    url = "http://127.0.0.1:%d" % lanloom_stub.server_address[1]
    link = transport.Link(url, str(tmp_path), __version__)
    link.cred = {"server": url, "token": "x"}
    lanloom_stub.answer = lambda path, body: (401, {"error": "auth.err.required"})
    link.queue({"t": 1})
    assert link.flush(PROTO) == "revoked" and link.revoked
    # the loop then stays quiet (no restart loop of the container) and forgets the token
    link._write("token.json", {"server": url, "token": "x"})
    link.cred = link._load()
    monkeypatch.setattr(runner, "REPORT_S", 0)
    monkeypatch.setattr(runner.Collector, "poll", lambda self: None)
    stops = iter([False] * 40 + [True] * 1000)
    assert runner.run(link, "", stop=lambda: next(stops), sleep=lambda s: None) == 3
    assert not link.paired and not (tmp_path / "token.json").exists()


def test_link_refuses_forbidden_fields_before_sending(tmp_path):
    link = transport.Link("http://127.0.0.1:9", str(tmp_path), __version__)
    link.queue({"t": 1, "containers": {"items": [{"name": "a", "env": ["X=" + SECRETS[1]]}]}})
    assert SECRETS[1] not in json.dumps(list(link.buffer))


def test_server_address_forms():
    assert transport.normalise_server("http://192.0.2.10:7380/") == "http://192.0.2.10:7380"
    assert transport.normalise_server("https://user:pw@x") == ""  # secret-scan: fake
    assert transport.normalise_server("ftp://x") == ""


# ---------------------------------------------------------------------- server: pairing, token, report, revoke (app)
@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    application = create_app(config_mod.load())
    with TestClient(application, base_url="http://lanloom.test") as c:
        st = application.state.ll
        c.st = st
        uid = st.accounts.create("admin-user", "a-long-test-password", role="admin")
        c.admin_token = st.sessions.create(uid, ip="127.0.0.1", agent="test")
        yield c


def _as_admin(c):
    c.cookies.clear()
    c.cookies.set(auth.SESSION_COOKIE, c.admin_token)


def _report(t=None, **extra):
    rep = {"t": t or time.time(), "interval": 10, "flows": [],
           "host": {"name": "web-host", "ips": ["192.168.50.40"], "macs": ["02:4c:4c:00:00:40"], "cpus": 2},
           "system": {"load": [0.5, 0.4, 0.3], "mem_total": 1000, "mem_avail": 400, "disks": [{"mount": "/", "total": 100, "free": 5}]},
           "units": {"available": True, "failed": [{"name": "backup.service", "since": 1, "result": "exit-code"}], "timers": []},
           "containers": {"available": True, "runtime": "docker", "items": [
               {"id": "a" * 12, "name": "web", "image": "example/web:1", "state": "running", "created": 1, "health": "healthy",
                "Env": ["X=" + SECRETS[1]], "tx": 10, "rx": 20}]}}
    rep.update(extra)
    return rep


def test_pairing_token_report_and_revoke(app):
    _as_admin(app)
    r = app.post("/api/agents/invite", headers=HDR, json={"name": "web host"})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", code) and r.json()["image"].endswith(":" + __version__)
    app.cookies.clear()
    bad = app.post("/api/agent/pair", headers=HDR, json={"code": "ZZZZ-ZZZZ", "proto": PROTO})
    assert bad.status_code == 403 and bad.json()["error"] == "agent.err.code"
    old = app.post("/api/agent/pair", headers=HDR, json={"code": code, "proto": 99})
    assert old.status_code == 409 and old.json()["error"] == "agent.err.proto"
    p = app.post("/api/agent/pair", headers=HDR, json={"code": code, "proto": PROTO, "version": __version__,
                                                        "host": {"name": "web-host", "ips": ["192.168.50.40"]}})
    assert p.status_code == 200, p.text
    token, aid = p.json()["token"], p.json()["agent_id"]
    again = app.post("/api/agent/pair", headers=HDR, json={"code": code, "proto": PROTO})
    assert again.status_code == 403                                      # one-time code
    # only the hash is stored, nowhere the token itself
    rows = app.st.store.query("SELECT * FROM agent_hosts")
    assert rows[0]["token_hash"] == agents_mod.token_hash(token) and token not in json.dumps([dict(r) for r in rows])
    dump = "\n".join(app.st.store._db.iterdump())
    assert token not in dump
    # reports need the token and are filtered once more
    assert app.post("/api/agent/report", headers=HDR, json={"proto": PROTO, "reports": []}).status_code == 401
    auth_h = dict(HDR, Authorization="Bearer " + token)
    r = app.post("/api/agent/report", headers=auth_h, json={"proto": PROTO, "version": "1.0.0", "reports": [_report()]})
    assert r.status_code == 200 and r.json()["server_version"] == __version__
    assert app.post("/api/agent/report", headers=auth_h, json={"proto": 2, "reports": []}).status_code == 409
    _as_admin(app)
    run = app.get("/api/running").json()
    host = run["hosts"][0]
    assert host["name"] == "web host" and host["online"] and host["update"]["image"] == "ghcr.io/robeertm/lanloom:" + __version__
    assert host["units"]["failed"][0]["name"] == "backup.service"
    assert SECRETS[1] not in json.dumps(run) and "Env" not in json.dumps(run)
    assert SECRETS[1] not in "\n".join(app.st.store._db.iterdump())
    lst = app.get("/api/agents").json()["agents"]
    assert lst[0]["id"] == aid and "token" not in json.dumps(lst) and "hash" not in json.dumps(lst)
    # revoke: the next report is refused
    assert app.delete("/api/agents/" + aid, headers=HDR).status_code == 200
    app.cookies.clear()
    assert app.post("/api/agent/report", headers=auth_h, json={"proto": PROTO, "reports": [_report()]}).status_code == 401
    acts = [r["action"] for r in app.st.store.query("SELECT action FROM audit")]
    assert "agents.invite" in acts and "agents.paired" in acts and "agents.revoke" in acts


def test_report_limits(app):
    hub = app.st.core.agents
    with pytest.raises(ValueError):
        hub.ingest("x", {"proto": PROTO, "reports": [{}] * (agents_mod.MAX_REPORTS + 1)})
    with pytest.raises(ValueError):
        hub.ingest("x", {"proto": PROTO})


def test_invite_code_expires(app):
    hub = app.st.core.agents
    code = hub.invite()["code"]
    real = hub.clock
    hub.clock = lambda: real() + agents_mod.CODE_VALID_S + 5
    try:
        with pytest.raises(PermissionError):
            hub.pair(code, {"proto": PROTO})
    finally:
        hub.clock = real


def test_roles_of_the_agent_routes(app):
    st = app.st
    uid = st.accounts.create("viewer-user", "a-long-test-password", role="viewer")
    app.cookies.set(auth.SESSION_COOKIE, st.sessions.create(uid, ip="127.0.0.1", agent="t"))
    assert app.get("/api/running").status_code == 200 and app.get("/api/agents").status_code == 200
    assert app.post("/api/agents/invite", headers=HDR, json={}).status_code == 403
    assert app.post("/api/agents/proxy", headers=HDR, json={"url": "tcp://192.0.2.1:2375"}).status_code == 403


def test_container_source_over_a_socket_proxy(app, runtime_sock):
    path, srv = runtime_sock
    _as_admin(app)
    t = app.post("/api/agents/proxy/test", headers=HDR, json={"url": "unix://" + path}).json()
    assert t["ok"] and t["containers"] == 2
    added = app.post("/api/agents/proxy", headers=HDR, json={"name": "box", "url": "unix://" + path}).json()
    hub = app.st.core.agents
    hub.poll_proxies()
    run = hub.running()
    h = next(x for x in run["hosts"] if x["id"] == added["id"])
    assert [c["name"] for c in h["containers"]["items"]] == ["web"] and h["containers"]["short_lived"] == 1
    assert {m for m, _ in srv.seen} == {"GET"}
    assert all(s not in json.dumps(run) for s in SECRETS)
    assert app.post("/api/agents/proxy", headers=HDR, json={"url": "http://u:p@192.0.2.1:2375"}).status_code == 400  # secret-scan: fake


# ---------------------------------------------------------------------- noise filter, conversations, samples, events
def test_noise_filter():
    now = 10_000.0
    assert agents_mod.short_lived({"state": "running", "created": now - 60}, now)
    assert not agents_mod.short_lived({"state": "running", "created": now - 700}, now)
    assert agents_mod.short_lived({"state": "exited", "created": now - 5000, "finished": now - 4900}, now)
    assert not agents_mod.short_lived({"state": "exited", "created": now - 5000, "finished": now - 100}, now)
    st = {"known_names": ["web"], "containers": {"items": [
        {"name": "web", "state": "running", "created": now - 10},                    # recreated, but known by name
        {"name": "job", "state": "exited", "created": now - 100, "finished": now - 50},
        {"name": "loop", "state": "restarting", "created": now - 30},
        {"name": "sick", "state": "running", "created": now - 30, "health": "unhealthy"},
        {"name": "new", "state": "running", "created": now - 30}]}}
    assert [c["name"] for c in agents_mod.visible(st, now)] == ["web", "loop", "sick"]


def test_conversation_booked_once_when_both_ends_run_an_agent(app):
    core, hub = app.st.core, app.st.core.agents
    now = time.time()
    core.ingest({"hosts": [{"ip": "192.168.50.%d" % n, "mac": "02:4c:4c:00:00:%02x" % n, "hostnames": [], "ports": []} for n in (40, 41)]}, None)
    core._refresh_index()
    a, b = core.ip_to_device["192.168.50.40"], core.ip_to_device["192.168.50.41"]
    for aid, ip in (("ag-a", "192.168.50.40"), ("ag-b", "192.168.50.41")):
        app.st.store.execute("INSERT INTO agent_hosts(id, kind, name, created, facts) VALUES(?,?,?,?,?)",
                             (aid, "agent", aid, now, json.dumps({"ips": [ip]})))
        hub.live[aid] = {"known_names": [], "t": now, "seen": now}
        hub._resolve_device(aid, {"ips": [ip]})
    first, second = sorted((("ag-a", a, "192.168.50.40", "192.168.50.41"), ("ag-b", b, "192.168.50.41", "192.168.50.40")),
                           key=lambda x: x[1])
    for aid, _dev, lip, rip in (first, second):
        hub.ingest_report(aid, {"t": now, "flows": [{"lip": lip, "rip": rip, "port": 445, "dir": "out", "tx": 50_000, "rx": 1_000}]}, now)
    talk = hub.talk(now)
    assert len(talk) == 1
    assert talk[0]["ab"] + talk[0]["ba"] == 51_000                       # one side only, not 102 000
    view = app.st.core.agents.containers_by_device(now)
    assert view == {}
    # the pair goes into the data paths: the map's "Who talks to whom" lines and the cards' "Where does it talk to?"
    pairs = core.paths.pairs("24h", now + 1)
    assert [(p["a"], p["b"], p["ab"] + p["ba"]) for p in pairs] == [(min(a, b), max(a, b), 51_000)]


def test_samples_and_health(app):
    hub = app.st.core.agents
    now = time.time()
    app.st.store.execute("INSERT INTO agent_hosts(id, kind, name, created, facts) VALUES(?,?,?,?,?)",
                         ("ag-s", "agent", "s", now, json.dumps({"cpus": 2})))
    hub.live["ag-s"] = {"known_names": [], "t": 0}
    hub.ingest_report("ag-s", _report(now), now)
    h = hub.health("ag-s", 1, now)
    pt = h["points"][-1]
    assert pt[1] == 0.5 and pt[2] == 2 and pt[3] == 60.0 and pt[4] == 95.0 and pt[5] == 1 and pt[6] == 1


def test_events_for_failed_units_unhealthy_and_offline(app):
    hub, core = app.st.core.agents, app.st.core
    sent = []
    core.notify.emit = lambda eid, key, data=None, **kw: sent.append((eid, key, kw.get("resolve", False)))
    now = time.time()
    app.st.store.execute("INSERT INTO agent_hosts(id, kind, name, created, last_seen, facts) VALUES(?,?,?,?,?,?)",
                         ("ag-e", "agent", "e", now, now, "{}"))
    rep = _report(now)
    rep["containers"]["items"][0].update(health="unhealthy")
    hub.live["ag-e"] = {"known_names": [], "t": 0}
    hub.ingest_report("ag-e", rep, now)
    hub.watch(now)
    assert ("host_unit_failed", "host_unit_failed:ag-e:backup.service", False) in sent
    assert ("container_unhealthy", "container_unhealthy:ag-e:web", False) in sent
    app.st.store.execute("UPDATE agent_hosts SET last_seen=? WHERE id='ag-e'", (now - 1000,))
    hub.live["ag-e"]["seen"] = now - 1000
    hub.watch(now)
    assert ("agent_offline", "agent_offline:ag-e", False) in sent
    assert ("host_unit_failed", "host_unit_failed:ag-e:backup.service", True) in sent


def test_nightly_section(app):
    from lanloom.nightly import hostsection
    from lanloom.nightly.contrib import Ctx
    hub = app.st.core.agents
    now = time.time()
    app.st.store.execute("INSERT INTO agent_hosts(id, kind, name, created, last_seen, version, facts) VALUES(?,?,?,?,?,?,?)",
                         ("ag-n", "agent", "n", now, now, "1.0.0", "{}"))
    hub.live["ag-n"] = {"known_names": [], "t": 0}
    hub.ingest_report("ag-n", _report(now), now)
    ctx = Ctx(core=app.st.core, journal=None, since=now - 86400, until=now, prev=None)
    assert hostsection._available(ctx)
    sec = hostsection._build(ctx)[0]
    keys = {i["key"] for i in sec["items"]}
    assert {"report.hosts.failed", "report.hosts.disk", "report.hosts.update"} <= keys and sec["level"] == "warn"


def test_demo_fills_running_view_and_map(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    application = create_app(config_mod.load())
    with TestClient(application, base_url="http://lanloom.test"):
        core = application.state.ll.core
        core.demo.seed(core)
        core._refresh_index()
        import random
        from lanloom import demo_agents, mapmodel
        demo_agents.seed(core)
        now = time.time()
        for i in range(3):
            demo_agents.tick(core, now + i * 10, 30 + i * 10, random.Random(1))
        run = core.agents.running(now + 30)
        names = {h["name"]: h for h in run["hosts"]}
        assert set(names) == {"storage", "backup", "homeserver"}
        assert names["storage"]["containers"]["short_lived"] == 1
        assert names["backup"]["units"]["failed"] and names["backup"]["update"]
        assert any(c["state"] == "restarting" for c in names["homeserver"]["containers"]["items"])
        net = mapmodel.build(core, "network")
        cts = [c for c in net["components"] if c["kind"] == "container"]
        assert cts and all(c.get("fold") for c in cts)
        assert core.paths.pairs("24h", now + 60)                   # conversations of the agents in the data paths
        assert len(core.agents.health(demo_agents.STORAGE, 24 * 7)["points"]) > 1000
