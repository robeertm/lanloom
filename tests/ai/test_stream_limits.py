"""The real request factory against a local test server: connect / first byte / idle / done limits."""
import http.server
import json
import socket
import threading
import time

import pytest

from lanloom.ai.providers import AIError, Client, Limits, _Stream

LINE = json.dumps({"message": {"content": "x"}}) + "\n"
LAST = json.dumps({"message": {"content": "end"}, "done": True}) + "\n"


class Server:
    def __init__(self, script):
        stop = self.stop = threading.Event()

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                script(self, stop)

            do_POST = do_GET = _go

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)


@pytest.fixture()
def serve():
    started = []

    def make(script):
        s = Server(script)
        started.append(s)
        return s
    yield make
    for s in started:
        s.close()


def head(h, status=200):
    h.send_response(status)
    h.send_header("Content-Type", "application/x-ndjson")
    h.end_headers()


def write(h, text):
    h.wfile.write(text.encode())
    h.wfile.flush()


def chat(url, limits):
    return Client("ollama", url, limits=limits).chat("s", "u", "m")


def test_port_closed_fails_fast():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    t0 = time.monotonic()
    with pytest.raises(AIError) as e:
        chat("http://127.0.0.1:%d" % port, Limits(connect=3, first=30, done=30, idle=30))
    assert e.value.key == "ai.err.unreachable" and time.monotonic() - t0 < 2


def test_accepts_but_sends_nothing_is_no_answer(serve):
    srv = serve(lambda h, stop: stop.wait(10))
    t0 = time.monotonic()
    with pytest.raises(AIError) as e:
        chat(srv.url, Limits(connect=2, first=0.3, done=30, idle=30))
    el = time.monotonic() - t0
    assert e.value.key == "ai.err.no_answer" and 0.25 <= el < 3


def test_stall_after_first_line_is_a_timeout(serve):
    def script(h, stop):
        head(h)
        write(h, LINE)
        stop.wait(10)
    srv = serve(script)
    t0 = time.monotonic()
    with pytest.raises(AIError) as e:
        chat(srv.url, Limits(connect=2, first=2, done=30, idle=0.5))
    el = time.monotonic() - t0
    assert e.value.key == "ai.err.timeout" and el < 4


def test_slow_but_steady_succeeds(serve):
    def script(h, stop):
        head(h)
        for _ in range(6):
            write(h, LINE)
            time.sleep(0.15)
        write(h, LAST)
    srv = serve(script)
    res = chat(srv.url, Limits(connect=2, first=2, done=10, idle=0.6))
    assert res["text"] == "x" * 6 + "end"


def test_first_byte_may_take_long_if_first_allows_it(serve):
    """Loading a model is load, not a hang: the long wait before the first byte is covered by ``first``."""
    def script(h, stop):
        time.sleep(0.8)
        head(h)
        write(h, LAST)
    srv = serve(script)
    assert chat(srv.url, Limits(connect=2, first=5, done=10, idle=0.5))["text"] == "end"


def test_steady_but_endless_hits_done(serve):
    def script(h, stop):
        head(h)
        for _ in range(100):
            if stop.is_set():
                return
            try:
                write(h, LINE)
            except OSError:
                return
            time.sleep(0.1)
    srv = serve(script)
    t0 = time.monotonic()
    with pytest.raises(AIError) as e:
        chat(srv.url, Limits(connect=2, first=2, done=0.8, idle=0.6))
    assert e.value.key == "ai.err.timeout" and time.monotonic() - t0 < 4


@pytest.mark.parametrize("status,body,key", [
    (401, "bad key", "ai.err.auth"), (404, "model 'm' not found", "ai.err.model"), (404, "nope", "ai.err.not_found"),
    (429, "slow down", "ai.err.rate"), (500, "oops", "ai.err.http")])
def test_http_errors_over_the_wire(serve, status, body, key):
    def script(h, stop):
        h.send_response(status)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        write(h, body)
    srv = serve(script)
    with pytest.raises(AIError) as e:
        chat(srv.url, Limits(connect=2, first=2, done=5, idle=2))
    assert e.value.key == key and e.value.status == status and body not in e.value.detail


def test_models_over_the_wire(serve):
    doc = json.dumps({"models": [{"name": "llama3.1:8b"}]})

    def script(h, stop):
        h.send_response(200)
        h.send_header("Content-Length", str(len(doc)))
        h.end_headers()
        write(h, doc)
    srv = serve(script)
    assert Client("ollama", srv.url).models() == ["llama3.1:8b"]


def test_request_is_sent_as_built(serve):
    seen = {}

    def script(h, stop):
        seen.update(path=h.path, method=h.command, auth=h.headers.get("Authorization"), ua=h.headers.get("User-Agent"))
        head(h)
        write(h, LAST)
    srv = serve(script)
    st = _Stream(srv.url + "/x?a=1", "POST", {"Authorization": "Bearer t", "User-Agent": "u"}, b"{}", Limits(2, 2, 2, 2))
    assert st.read_all() == LAST.rstrip("\n").encode()
    st.close()
    assert seen == {"path": "/x?a=1", "method": "POST", "auth": "Bearer t", "ua": "u"}


def test_oversized_answer_is_cut(serve, monkeypatch):
    from lanloom.ai import providers
    monkeypatch.setattr(providers, "MAX_BODY", 4096)

    def script(h, stop):
        head(h)
        try:
            for _ in range(100):
                write(h, "y" * 1000 + "\n")
        except OSError:
            pass
    srv = serve(script)
    st = _Stream(srv.url, "GET", {}, None, Limits(2, 2, 5, 2))
    with pytest.raises(AIError) as e:
        list(st.lines())
    assert e.value.key == "ai.err.too_large"
    st.close()
