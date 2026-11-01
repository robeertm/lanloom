"""Tests of the runtime API client against an in-process fake unix-socket HTTP server."""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import struct
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from updater import runtimeapi as R  # noqa: E402

pytestmark = pytest.mark.skipif(not hasattr(socketserver, "ThreadingUnixStreamServer"),
                                reason="unix sockets not available")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fake"
    routes: dict = {}
    seen: list = []

    def log_message(self, *a):  # silence
        pass

    def address_string(self):
        return "local"

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def _send(self, status, obj=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _chunked(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for c in chunks:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(c), c))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    def _route(self, method):
        body = self._body()
        Handler.seen.append((method, self.path, body))
        path = self.path.split("?", 1)[0]
        if path == "/version":
            return self._send(200, {"ApiVersion": "1.55", "MinAPIVersion": "1.44", "Version": "x"})
        key = (method, path)
        if key not in Handler.routes:
            return self._send(404, {"message": "No such thing: " + path})
        action = Handler.routes[key]
        action(self)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


@pytest.fixture
def server():
    d = tempfile.mkdtemp(prefix="llu")
    sock = os.path.join(d, "s.sock")
    Handler.routes = {}
    Handler.seen = []
    srv = socketserver.ThreadingUnixStreamServer(sock, Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield R.RuntimeAPI(sock, timeout=5)
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(d, ignore_errors=True)


def test_negotiate_version_rules():
    assert R.negotiate_version({"ApiVersion": "1.55", "MinAPIVersion": "1.44"}) == (1, 47)
    assert R.negotiate_version({"ApiVersion": "1.43", "MinAPIVersion": "1.12"}) == (1, 43)
    assert R.negotiate_version({"ApiVersion": "1.60", "MinAPIVersion": "1.50"}) == (1, 50)
    assert R.negotiate_version({}) == (1, 41)
    assert R.negotiate_version({"ApiVersion": "garbage"}) == (1, 41)


def test_requests_are_versioned_and_errors_mapped(server):
    Handler.routes[("GET", "/v1.47/containers/app/json")] = lambda h: h._send(200, {"Id": "abc"})
    Handler.routes[("POST", "/v1.47/containers/create")] = lambda h: h._send(201, {"Id": "new1"})
    Handler.routes[("POST", "/v1.47/containers/app/start")] = lambda h: h._send(409, {"message": "busy"})
    Handler.routes[("POST", "/v1.47/containers/app/stop")] = lambda h: h._send(304)
    assert server.container_inspect("app") == {"Id": "abc"}
    assert server.api_version == (1, 47)
    assert server.container_create("lanloom", {"Image": "x"}) == "new1"
    method, path, body = Handler.seen[-1]
    assert path == "/v1.47/containers/create?name=lanloom" and body == {"Image": "x"}
    with pytest.raises(R.Conflict) as exc:
        server.container_start("app")
    assert exc.value.message == "busy"
    server.container_stop("app", 5)  # 304 = already stopped, not an error
    assert Handler.seen[-1][1] == "/v1.47/containers/app/stop?t=5"
    with pytest.raises(R.NotFound):
        server.container_inspect("missing")
    assert server.image_inspect("registry.example/acme/lanloom:1.0.0") is None
    assert Handler.seen[-1][1] == "/v1.47/images/registry.example/acme/lanloom:1.0.0/json"


def test_container_list_filters(server):
    Handler.routes[("GET", "/v1.47/containers/json")] = lambda h: h._send(200, [{"Id": "a"}])
    assert server.containers(labels=["org.lanloom.role=app"]) == [{"Id": "a"}]
    path = Handler.seen[-1][1]
    assert "all=1" in path and "filters=" in path and "org.lanloom.role%3Dapp" in path


def test_pull_stream_parsing(server):
    chunks = [
        b'{"status":"Pulling from acme/lanloom"}\r\n{"status":"Downl',
        b'oading","progress":"[=>  ]"}\n\n',
        b'garbage line\n',
        b'{"status":"a"}{"status":"b"}\n',
        b'{"status":"Digest: sha256:00"}\n{"status":"Status: Downloaded newer image"}',
    ]
    Handler.routes[("POST", "/v1.47/images/create")] = lambda h: h._chunked(chunks)
    seen = []
    last = server.image_pull("registry.example/acme/lanloom", "1.2.3", seen.append)
    assert [o["status"] for o in seen] == ["Pulling from acme/lanloom", "Downloading", "a", "b",
                                           "Digest: sha256:00", "Status: Downloaded newer image"]
    assert last["status"].startswith("Status:")
    assert "fromImage=registry.example%2Facme%2Flanloom&tag=1.2.3" in Handler.seen[-1][1]


def test_pull_stream_error(server):
    chunks = [b'{"status":"Pulling"}\n', b'{"errorDetail":{"message":"manifest unknown"},"error":"manifest unknown"}\n']
    Handler.routes[("POST", "/v1.47/images/create")] = lambda h: h._chunked(chunks)
    with pytest.raises(R.RuntimeAPIError) as exc:
        server.image_pull("registry.example/acme/lanloom", "9.9.9")
    assert "manifest unknown" in exc.value.message


def test_pull_http_error(server):
    Handler.routes[("POST", "/v1.47/images/create")] = lambda h: h._send(500, {"message": "denied"})
    with pytest.raises(R.RuntimeAPIError) as exc:
        server.image_pull("registry.example/acme/lanloom", "9.9.9")
    assert exc.value.message == "denied"


def test_logs_are_demultiplexed(server):
    frames = b"".join(struct.pack(">BxxxI", s, len(t)) + t for s, t in
                      [(1, b"hello\n"), (2, b"warn line\n"), (1, b"bye\n")])
    Handler.routes[("GET", "/v1.47/containers/x/logs")] = lambda h: h._send(200, raw=frames,
                                                                             ctype="application/octet-stream")
    assert server.container_logs("x", tail=2) == ["warn line", "bye"]


def test_unreachable_socket(tmp_path):
    api = R.RuntimeAPI(str(tmp_path / "nope.sock"), timeout=1)
    with pytest.raises(R.Unreachable):
        api.negotiate()


def test_json_stream_unit():
    p = R.JSONStream()
    assert p.feed(b'{"a":1}\n{"b"') == [{"a": 1}]
    assert p.feed(b':2}\r\n[1,2]\n') == [{"b": 2}, [1, 2]]
    assert p.feed("{\"u\":\"\u00e9\"}\n".encode()[:8]) == []
    assert p.feed("{\"u\":\"\u00e9\"}\n".encode()[8:]) == [{"u": "\u00e9"}]
    assert p.feed(b"x" * (R.MAX_STREAM_LINE + 10)) == []
    assert p.feed(b'\n{"c":3}') == []
    assert p.close() == [{"c": 3}]


def test_demux_raw_passthrough():
    assert R.demux_logs(b"plain text\n") == "plain text\n"
