import asyncio
import http.server
import threading

import pytest

from lanloom.scan import banners, httpget


def test_extract_title():
    assert banners.extract_title(b"<html><head><TITLE> Hello\n  World </TITLE></head>") == "Hello World"
    assert banners.extract_title(b"<title>A &amp; B</title>") == "A & B"
    assert banners.extract_title(b"<title lang='x'>caf\xc3\xa9</title>") == "café"
    assert banners.extract_title(b"<title>caf\xe9</title>", "text/html; charset=iso-8859-1") == "café"
    assert banners.extract_title(b"<title>x</title>", "text/html; charset=bogus-enc") == "x"
    assert banners.extract_title(b"<title></title>") is None
    assert banners.extract_title(b"no title here") is None
    assert banners.extract_title(b"") is None
    assert len(banners.extract_title(b"<title>" + b"a" * 5000 + b"</title>")) == banners.MAX_TEXT
    assert banners.extract_title(b"<title>a\x00\x1bb</title>") == "a b"


def test_parse_head():
    status, headers = httpget.parse_head(b"HTTP/1.1 301 Moved\r\nLocation: /x\r\nServer: a\r\nserver: b\r\nbad line")
    assert status == 301 and headers == {"location": "/x", "server": "a"}
    assert httpget.parse_head(b"garbage")[0] == 0
    assert httpget.parse_head(b"HTTP/1.1 abc OK")[0] == 0


def test_same_host_url():
    assert httpget.same_host_url("192.168.1.5", "https://192.168.1.5:8443/login")
    assert not httpget.same_host_url("192.168.1.5", "http://192.168.1.6/")
    assert not httpget.same_host_url("192.168.1.5", "http://example.invalid/")
    assert not httpget.same_host_url("192.168.1.5", "ftp://192.168.1.5/")


class _Server:
    def __init__(self, routes):
        routes_ = routes

        class H(http.server.BaseHTTPRequestHandler):
            server_version = "TestServer/1.0"
            sys_version = ""

            def do_GET(self):
                status, headers, body = routes_.get(self.path, (404, {}, b""))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_fetch_banner_localhost():
    with _Server({"/": (200, {"Content-Type": "text/html"}, b"<title>Test device</title>")}) as s:
        b = asyncio.run(banners.fetch_banner("127.0.0.1", s.port, tls=False))
    assert b == {"title": "Test device", "server": "TestServer/1.0"}


def test_redirect_same_host_followed():
    with _Server({"/": (302, {"Location": "/ui"}, b""),
                  "/ui": (200, {}, b"<title>UI</title>")}) as s:
        b = asyncio.run(banners.fetch_banner("127.0.0.1", s.port, tls=False))
    assert b["title"] == "UI"


def test_redirect_other_host_not_followed():
    with _Server({"/": (302, {"Location": "http://192.0.2.1/elsewhere"}, b"<title>moved</title>")}) as s:
        res = asyncio.run(httpget.http_get("127.0.0.1", s.port))
    assert res.status == 302 and res.url.endswith("/")


def test_redirect_loop_bounded():
    with _Server({"/": (302, {"Location": "/"}, b"")}) as s:
        res = asyncio.run(httpget.http_get("127.0.0.1", s.port, timeout=2.0))
    assert res is None


def test_body_cap():
    big = b"<title>big</title>" + b"x" * 200_000
    with _Server({"/": (200, {}, big)}) as s:
        res = asyncio.run(httpget.http_get("127.0.0.1", s.port, cap=1024))
    assert res.status == 200 and len(res.body) <= 1024


def test_closed_port_returns_none():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert asyncio.run(banners.fetch_banner("127.0.0.1", port, tls=False)) is None


def test_silent_server_times_out():
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        import time

        async def go():
            # the outer guard is 40x the timeout: a slow runner cannot trip it, an ignored timeout always does
            return await asyncio.wait_for(httpget.http_get("127.0.0.1", port, timeout=0.5), 20)

        t0 = time.monotonic()
        assert asyncio.run(go()) is None
        assert time.monotonic() - t0 >= 0.4     # it really waited for the silent server (a lower bound never flakes)
    finally:
        srv.close()


def test_tls_against_plain_server_fails_cleanly():
    with _Server({"/": (200, {}, b"<title>x</title>")}) as s:
        assert asyncio.run(banners.fetch_banner("127.0.0.1", s.port, tls=True, timeout=1.0)) is None


@pytest.mark.parametrize("raw", [b"", b"\r\n\r\n", b"HTTP/1.1 200 OK\r\n\r\n", b"HTTP/1.0 200\nServer: x\n\n<title>t</title>"])
def test_odd_responses(raw):
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        try:
            c, _ = srv.accept()
            c.recv(4096)
            c.sendall(raw)
            c.close()
        except OSError:
            pass

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    try:
        asyncio.run(banners.fetch_banner("127.0.0.1", port, tls=False, timeout=1.0))   # must not raise
    finally:
        th.join(2)
        srv.close()
