"""Unix-socket addresses (uvicorn 0.5x reports the server of a unix socket as (path, None) where older versions gave
None; the client is None). Every check that reads a peer or builds a URL must treat both forms the same way:
  * auth.peer_host() turns every form into a host or "" (a socket path never counts as an address);
  * main.SocketAddresses gives every layer behind it the classic form, so the framework never builds a URL with the
    socket path as its authority - the access guard reads that URL's path;
  * end to end with a real uvicorn on a unix socket: health answers, the API stays closed without a session, also for
    an HTTP/1.0 request without a Host header, and the add-on gate refuses a socket peer."""
import asyncio
import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections import namedtuple

import pytest
import uvicorn

from lanloom import auth
from lanloom import config as config_mod
from lanloom.main import SocketAddresses, create_app

Address = namedtuple("Address", "host port")


@pytest.mark.parametrize("addr, host", [
    (None, ""), ((), ""), (("192.168.50.10", 51000), "192.168.50.10"), (["::1", 1], "::1"),
    (("/run/lanloom.sock", None), ""), (("", 0), ""), (("testclient", 50000), "testclient"),
    (Address("192.168.50.11", 2), "192.168.50.11"), (("relative/sock", 0), ""), (5, ""), ("x", ""),
])
def test_peer_host_handles_every_form(addr, host):
    assert auth.peer_host(addr) == host


def test_a_socket_peer_is_never_trusted_nor_loopback():
    proxies = auth.parse_proxies("127.0.0.1/8, ::1")
    for addr in (("/run/lanloom.sock", None), None, ("", 0)):
        assert not auth.from_trusted_proxy(auth.peer_host(addr), proxies)
        assert auth._ip(auth.peer_host(addr)) is None


def _run(app, scope):
    seen = {}

    async def inner(sc, receive, send):
        seen.update(sc)

    asyncio.run(SocketAddresses(inner)(scope, None, None))
    return seen


@pytest.mark.parametrize("server, client, want_server, want_client", [
    (("/run/lanloom.sock", None), None, None, None),                    # uvicorn 0.5x over a unix socket
    (None, None, None, None),                                           # older uvicorn over a unix socket
    (("/run/lanloom.sock", None), ("/run/lanloom.sock", None), None, None),
    (("192.168.50.2", 7380), ("192.168.50.10", 50000), ("192.168.50.2", 7380), ("192.168.50.10", 50000)),   # TCP unchanged
])
def test_socket_addresses_layer_gives_one_form(server, client, want_server, want_client):
    seen = _run(None, {"type": "http", "path": "/api/health", "headers": [], "server": server, "client": client})
    assert seen["server"] == want_server and seen["client"] == want_client


def test_the_framework_url_keeps_the_path_without_a_host_header():
    from starlette.requests import Request
    raw = {"type": "http", "method": "GET", "path": "/api/devices", "query_string": b"", "headers": [],
           "server": ("/tmp/lanloom-test.sock", None), "client": None, "scheme": "http"}
    assert not Request(raw).url.path.startswith("/api/")           # the trap: socket path + "None" become the authority
    assert Request(_run(None, raw)).url.path == "/api/devices"      # after the layer the guard reads the real path


# ------------------------------------------------------------------ end to end: real uvicorn on a unix socket
class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=20)
        self.sock_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(20)
        self.sock.connect(self.sock_path)


def _raw10(path, target):
    """HTTP/1.0 without a Host header (allowed in 1.0); returns (status, headers text, body)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20)
    s.connect(path)
    s.sendall(("GET %s HTTP/1.0\r\n\r\n" % target).encode())
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), head.decode("latin-1"), body


class _Capture:
    """Outside the app: remembers what uvicorn itself put into the scope."""

    def __init__(self, app):
        self.app, self.servers = app, []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.servers.append(scope.get("server"))
        await self.app(scope, receive, send)


@pytest.fixture()
def served(tmp_path, monkeypatch, request):
    env = getattr(request, "param", {})
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    app = _Capture(create_app(config_mod.load()))
    folder = tempfile.mkdtemp(prefix="ll-uds-", dir="/tmp")            # unix socket paths are short (about 100 bytes)
    path = os.path.join(folder, "s")
    server = uvicorn.Server(uvicorn.Config(app, uds=path, log_level="warning", access_log=False, lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 60
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start on the unix socket"
    try:
        yield path, app
    finally:
        server.should_exit = True
        thread.join(30)
        shutil.rmtree(folder, ignore_errors=True)


def test_uvicorn_reports_the_socket_and_lanloom_answers(served):
    path, app = served
    c = _UnixHTTP(path)
    c.request("GET", "/api/health")
    r = c.getresponse()
    assert r.status == 200 and json.loads(r.read())["ok"] is True
    assert r.getheader("Cache-Control") == "no-store"                  # the API part of the guard ran
    assert app.servers and all(s is None or (s[1] is None and s[0] == path) for s in app.servers), app.servers


def test_the_api_stays_closed_over_a_socket_also_without_a_host_header(served):
    path, _ = served
    c = _UnixHTTP(path)
    c.request("GET", "/api/devices")
    r = c.getresponse()
    r.read()
    assert r.status == 401
    status, head, _ = _raw10(path, "/api/devices")
    assert status == 401, head
    status, head, _ = _raw10(path, "/api/health")
    assert status == 200 and "no-store" in head.lower()


@pytest.mark.parametrize("served", [{"LANLOOM_ADDON": "1", "LANLOOM_TRUSTED_PROXIES": "127.0.0.1"}], indirect=True)
def test_the_addon_gate_refuses_a_socket_peer(served):
    path, _ = served
    status, _, body = _raw10(path, "/api/health")
    assert status == 403 and b"add-on" in body
