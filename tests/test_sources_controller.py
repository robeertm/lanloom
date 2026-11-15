"""Switch / access point controller source: local fake controller over plain HTTP on 127.0.0.1."""
import asyncio
import json
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lanloom.sources import SOURCES, REASON_KEYS, SourceError
from lanloom.sources import controller as m

USER = "viewer"
PW = "pw-Q7r2x"
SESSION = "sess"                 # cookie value prefix handed out by the fake
CSRF = "csrf-Z4k8"

GW, SW, AP1, AP2, EXT = ("02:00:00:00:00:%02x" % i for i in (0x10, 0x01, 0x02, 0x03, 0x04))

# device type codes below are API identifiers
DEVICES = [
    {"mac": GW, "type": "ugw", "name": "Gateway", "model": "GW1", "ip": "192.168.50.1"},
    {"mac": SW, "type": "usw", "name": "Switch Rack", "model": "SW8", "ip": "192.168.50.3",
     "uplink": {"type": "wire", "uplink_mac": GW, "uplink_remote_port": 3, "port_idx": 8}},
    {"mac": AP1.upper(), "type": "uap", "name": "AP Hall", "model": "AP1", "ip": "192.168.50.4",
     "uplink": {"type": "wire", "uplink_mac": SW, "uplink_remote_port": 5, "port_idx": 1}},
    {"mac": AP2, "type": "uap", "name": "AP Garden", "model": "AP2", "ip": "192.168.50.5",
     "uplink": {"type": "wireless", "uplink_mac": AP1}},
    {"mac": EXT, "type": "uex", "name": "Extender", "ip": "192.168.50.6",
     "last_uplink": {"type": "wire", "uplink_mac": SW, "uplink_remote_port": 2}},
    {"mac": "not-a-mac", "type": "usw"},
]


def sw_client(n, **kw):
    return dict({"mac": "02:00:00:00:01:%02x" % n, "ip": "192.168.50.%d" % (100 + n), "is_wired": True}, **kw)


CLIENTS = [
    sw_client(1, name="nas", sw_mac=SW, sw_port=2),
    sw_client(2, mac="02-00-00-00-01-02", hostname="printer", sw_mac=SW, sw_port=4),
    sw_client(3, last_uplink_mac=SW, sw_port=6),
    sw_client(4, last_uplink_mac=SW),
    sw_client(5),                                                               # no parent -> skipped
    {"mac": "02:00:00:00:02:01", "ip": "192.168.50.121", "is_wired": False, "ap_mac": AP1, "radio": "na",
     "essid": "home-5", "name": "phone"},
    {"mac": "02:00:00:00:02:02", "is_wired": False, "ap_mac": AP2, "radio": "ng", "essid": "home-2g"},
    {"mac": "02:00:00:00:02:03", "is_wired": False, "ap_mac": AP2, "radio": "6e", "essid": "home-6"},
    {"mac": "02:00:00:00:02:04", "is_wired": False, "ap_mac": AP1, "channel": 6},
    {"mac": "02:00:00:00:02:05", "is_wired": False, "ap_mac": AP1, "channel": 100},
    {"mac": "02:00:00:00:02:06", "is_wired": False, "ap_mac": AP1},
    {"mac": "02:00:00:00:02:07", "is_wired": False},                             # no access point -> skipped
]


class Fake:
    """Fake controller; ``styles`` says which API generations it serves."""

    def __init__(self, styles=("console", "classic"), data_site="default"):
        self.styles = set(styles)
        self.data_site = data_site
        self.sta, self.dev = CLIENTS, DEVICES
        self.requests = []           # (method, path, cookie header, csrf header, body)
        self.sessions = set()
        self.counter = 0
        self.raw = ""                # json text instead of the normal reply (e.g. not JSON)
        self.bare = False            # console style answers with the bare list
        self.rc_error = ""           # meta.rc error message for data replies
        self.status_override = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def count(self, method, path_re):
        return sum(1 for r in self.requests if r[0] == method and re.search(path_re, r[1]))

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, obj=None, headers=(), text=None):
                body = (text if text is not None else json.dumps(obj if obj is not None else {})).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_POST(self):
                body = self.body()
                fake.requests.append(("POST", self.path, self.headers.get("Cookie"), self.headers.get("X-CSRF-Token"),
                                      body))
                style = {"/api/auth/login": "console", "/api/login": "classic"}.get(self.path)
                if self.path in ("/api/auth/logout", "/api/logout"):
                    fake.sessions.clear()
                    return self.reply(200)
                if style is None or style not in fake.styles:
                    return self.reply(404)
                if body.get("username") != USER or body.get("password") != PW:
                    return self.reply(401 if style == "console" else 400, {"meta": {"rc": "error"}})
                fake.counter += 1
                sid = "%s%d" % (SESSION, fake.counter)
                fake.sessions.add(sid)
                heads = [("Set-Cookie", "sid=%s; Path=/; HttpOnly; Secure" % sid)]
                if style == "console":
                    heads.append(("X-CSRF-Token", CSRF))
                else:
                    heads.append(("Set-Cookie", "csrf_token=%s; Path=/" % CSRF))
                self.reply(200, {"meta": {"rc": "ok"}}, heads)

            def do_GET(self):
                fake.requests.append(("GET", self.path, self.headers.get("Cookie"), self.headers.get("X-CSRF-Token"),
                                      None))
                mt = re.match(r"\A(/proxy/network)?/api/s/%s/stat/(sta|device)\Z" % fake.data_site, self.path)
                if not mt or ("console" if mt.group(1) else "classic") not in fake.styles:
                    return self.reply(404)
                cookie = self.headers.get("Cookie") or ""
                sid = re.search(r"sid=([^;]+)", cookie)
                if not sid or sid.group(1) not in fake.sessions:
                    return self.reply(401, {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}})
                if self.headers.get("X-CSRF-Token") != CSRF:
                    return self.reply(403)
                if fake.status_override:
                    return self.reply(fake.status_override)
                if fake.raw:
                    return self.reply(200, text=fake.raw)
                if fake.rc_error:
                    return self.reply(200, {"meta": {"rc": "error", "msg": fake.rc_error}, "data": []})
                data = fake.sta if mt.group(2) == "sta" else fake.dev
                self.reply(200, data if fake.bare and mt.group(1) else {"meta": {"rc": "ok"}, "data": data})

        return H


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.stop()


def make(f, **kw):
    kw.setdefault("timeout", 5.0)
    return m.Controller(f.url, USER, PW, **kw)


def run(coro):
    return asyncio.run(coro)


def key_of(exc):
    return exc.value.key


# ================================================================== mapping
def expected_clients():
    return [
        {"mac": "02:00:00:00:01:01", "ip": "192.168.50.101", "name": "nas", "medium": "wired", "parent_mac": SW,
         "port": "2", "band": None, "ssid": None},
        {"mac": "02:00:00:00:01:02", "ip": "192.168.50.102", "name": "printer", "medium": "wired", "parent_mac": SW,
         "port": "4", "band": None, "ssid": None},
        {"mac": "02:00:00:00:01:03", "ip": "192.168.50.103", "name": None, "medium": "wired", "parent_mac": SW,
         "port": "6", "band": None, "ssid": None},
        {"mac": "02:00:00:00:01:04", "ip": "192.168.50.104", "name": None, "medium": "wired", "parent_mac": SW,
         "port": None, "band": None, "ssid": None},
        {"mac": "02:00:00:00:02:01", "ip": "192.168.50.121", "name": "phone", "medium": "radio", "parent_mac": AP1,
         "port": None, "band": "5ghz", "ssid": "home-5"},
        {"mac": "02:00:00:00:02:02", "ip": None, "name": None, "medium": "radio", "parent_mac": AP2, "port": None,
         "band": "2.4ghz", "ssid": "home-2g"},
        {"mac": "02:00:00:00:02:03", "ip": None, "name": None, "medium": "radio", "parent_mac": AP2, "port": None,
         "band": "6ghz", "ssid": "home-6"},
        {"mac": "02:00:00:00:02:04", "ip": None, "name": None, "medium": "radio", "parent_mac": AP1, "port": None,
         "band": "2.4ghz", "ssid": None},
        {"mac": "02:00:00:00:02:05", "ip": None, "name": None, "medium": "radio", "parent_mac": AP1, "port": None,
         "band": "5ghz", "ssid": None},
        {"mac": "02:00:00:00:02:06", "ip": None, "name": None, "medium": "radio", "parent_mac": AP1, "port": None,
         "band": None, "ssid": None},
    ]


def expected_devices():
    return [
        {"mac": GW, "ip": "192.168.50.1", "name": "Gateway", "role": "router", "model": "GW1", "uplink": None},
        {"mac": SW, "ip": "192.168.50.3", "name": "Switch Rack", "role": "switch", "model": "SW8",
         "uplink": {"mac": GW, "port": "3", "own_port": "8", "medium": "wired"}},
        {"mac": AP1, "ip": "192.168.50.4", "name": "AP Hall", "role": "ap", "model": "AP1",
         "uplink": {"mac": SW, "port": "5", "own_port": "1", "medium": "wired"}},
        {"mac": AP2, "ip": "192.168.50.5", "name": "AP Garden", "role": "ap", "model": "AP2",
         "uplink": {"mac": AP1, "port": None, "own_port": None, "medium": "radio"}},
        {"mac": EXT, "ip": "192.168.50.6", "name": "Extender", "role": "other", "model": None,
         "uplink": {"mac": SW, "port": "2", "own_port": None, "medium": "wired"}},
    ]


@pytest.mark.parametrize("style", ["console", "classic"])
def test_event_shape_and_mapping(style):
    f = Fake(styles=(style,))
    try:
        ev = run(make(f).poll())
    finally:
        f.stop()
    assert set(ev) == {"type", "source", "t", "devices", "clients"}
    assert ev["type"] == "topology" and ev["source"] == "controller" and isinstance(ev["t"], float)
    for d in ev["devices"]:
        assert set(d) == {"mac", "ip", "name", "role", "model", "uplink"}
    assert ev["devices"] == [dict(d, model=d["model"]) for d in expected_devices()]
    assert ev["clients"] == expected_clients()


def test_bare_list_reply_is_accepted():
    f = Fake(styles=("console",))
    f.bare = True
    try:
        ev = run(make(f).poll())
    finally:
        f.stop()
    assert len(ev["clients"]) == 10 and len(ev["devices"]) == 5


def test_names_clipped_and_duplicates_dropped():
    f = Fake(styles=("classic",))
    f.sta = [sw_client(1, name="n" * 200 + "\x07", sw_mac=SW), sw_client(1, name="again", sw_mac=SW)]
    f.dev = []
    try:
        ev = run(make(f).poll())
    finally:
        f.stop()
    assert len(ev["clients"]) == 1 and ev["clients"][0]["name"] == "n" * 64


def test_limits():
    f = Fake(styles=("classic",))
    f.sta = [{"mac": "02:00:%02x:%02x:00:01" % (i // 256, i % 256), "is_wired": True, "sw_mac": SW}
             for i in range(5200)]
    f.dev = [{"mac": "02:01:%02x:%02x:00:01" % (i // 256, i % 256), "type": "usw"} for i in range(1100)]
    try:
        ev = run(make(f).poll())
    finally:
        f.stop()
    assert len(ev["clients"]) == 5000 and len(ev["devices"]) == 1000


def test_oversized_reply_is_protocol_error(fake, monkeypatch):
    monkeypatch.setattr(m, "MAX_BYTES", 500)
    with pytest.raises(SourceError) as e:
        run(make(fake).poll())
    assert key_of(e) == "source.controller.protocol"


# ================================================================== login, style detection, cookies, CSRF
def test_auto_detects_console_first(fake):
    c = make(fake)
    run(c.poll())
    assert [r[1] for r in fake.requests[:3]] == ["/api/auth/login", "/proxy/network/api/s/default/stat/sta",
                                                  "/proxy/network/api/s/default/stat/device"]
    assert c._style == "console" and fake.count("POST", r"\A/api/login\Z") == 0


def test_auto_falls_back_to_classic_after_404():
    f = Fake(styles=("classic",))
    try:
        c = make(f)
        run(c.poll())
        assert [(r[0], r[1]) for r in f.requests[:2]] == [("POST", "/api/auth/login"), ("POST", "/api/login")]
        assert c._style == "classic"
        n = len(f.requests)
        c._logged_in = False
        run(c.poll())                           # the style is remembered: no second console attempt
        assert [r[1] for r in f.requests[n:n + 1]] == ["/api/login"]
    finally:
        f.stop()


def test_forced_style_does_not_fall_back():
    f = Fake(styles=("classic",))
    try:
        with pytest.raises(SourceError) as e:
            run(make(f, style="console").poll())
        assert key_of(e) == "source.controller.protocol"
        assert all(r[1] == "/api/auth/login" for r in f.requests)
    finally:
        f.stop()


def test_cookie_and_csrf_round_trip():
    for styles in (("console",), ("classic",)):
        f = Fake(styles=styles)
        try:
            run(make(f).poll())
            gets = [r for r in f.requests if r[0] == "GET"]
            assert len(gets) == 2
            for _, _, cookie, csrf, _ in gets:
                assert re.search(r"sid=%s1" % SESSION, cookie) and csrf == CSRF
            if styles == ("classic",):
                assert "csrf_token=%s" % CSRF in gets[0][2]
            else:
                assert "HttpOnly" not in gets[0][2] and "Path" not in gets[0][2]   # name=value only
        finally:
            f.stop()


def test_wrong_password_is_auth_failed(fake):
    c = m.Controller(fake.url, USER, "wrong-pw", timeout=5)
    with pytest.raises(SourceError) as e:
        run(c.poll())
    assert key_of(e) == "source.controller.auth_failed"
    assert not [r for r in fake.requests if r[0] == "GET"]


def test_wrong_password_classic_status_400():
    f = Fake(styles=("classic",))
    try:
        with pytest.raises(SourceError) as e:
            run(m.Controller(f.url, USER, "wrong-pw", timeout=5).poll())
        assert key_of(e) == "source.controller.auth_failed"
    finally:
        f.stop()


def test_401_while_polling_relogs_in_once(fake):
    c = make(fake)
    run(c.poll())
    fake.sessions.clear()                       # the controller forgot our session
    ev = run(c.poll())
    assert len(ev["clients"]) == 10
    assert fake.count("POST", r"login\Z") == 2


def test_persistent_401_gives_up_after_one_relogin(fake, monkeypatch):
    c = make(fake)
    run(c.poll())
    fake.sessions.clear()
    orig = c._login

    async def login_without_session():
        await orig()
        fake.sessions.clear()                   # the new session is refused as well

    monkeypatch.setattr(c, "_login", login_without_session)
    with pytest.raises(SourceError) as e:
        run(c.poll())
    assert key_of(e) == "source.controller.auth_failed"
    assert fake.count("POST", r"login\Z") == 2


def test_missing_csrf_means_forbidden_for_the_data(fake):
    c = make(fake)
    run(c.poll())
    c._csrf = "other"                           # a token the controller does not know
    with pytest.raises(SourceError) as e:
        run(c.poll())
    assert key_of(e) == "source.controller.forbidden"


def test_non_json_is_protocol(fake):
    fake.raw = "<html>maintenance</html>"
    with pytest.raises(SourceError) as e:
        run(make(fake).poll())
    assert key_of(e) == "source.controller.protocol"


@pytest.mark.parametrize("raw", ['"text"', '{"data": {"a": 1}}', '{"meta": {"rc": "ok"}}', "42"])
def test_wrong_shape_is_protocol(fake, raw):
    fake.raw = raw
    with pytest.raises(SourceError) as e:
        run(make(fake).poll())
    assert key_of(e) == "source.controller.protocol"


def test_error_meta_and_server_error_are_protocol(fake):
    fake.rc_error = "api.err.Something"
    with pytest.raises(SourceError) as e:
        run(make(fake).poll())
    assert key_of(e) == "source.controller.protocol"
    fake.rc_error = ""
    fake.status_override = 500
    with pytest.raises(SourceError) as e:
        run(make(fake).poll())
    assert key_of(e) == "source.controller.protocol"


def test_site_is_part_of_the_path():
    f = Fake(styles=("classic",), data_site="lab-1")
    try:
        ev = run(make(f, site="lab-1").poll())
        assert ev["devices"]
        assert any(r[1] == "/api/s/lab-1/stat/sta" for r in f.requests)
    finally:
        f.stop()


# ================================================================== configuration
@pytest.mark.parametrize("kw", [
    {"url": ""}, {"url": "ftp://192.168.50.2"}, {"url": "https://user:pw@192.168.50.2"},  # secret-scan: fake
    {"url": "https://192.168.50.2:99999"}, {"username": ""}, {"username": None}, {"password": ""},
    {"password": 12345}, {"site": ""}, {"site": "a/b"}, {"site": "x" * 65}, {"site": "../etc"}, {"style": "other"},
])
def test_bad_config_is_refused(kw):
    args = dict(url="https://192.168.50.2", username=USER, password=PW)
    args.update(kw)
    with pytest.raises(SourceError) as e:
        m.Controller(**args)
    assert key_of(e) == "source.controller.config"


@pytest.mark.parametrize("url,expected", [
    ("https://192.168.50.2", (True, "192.168.50.2", 443)),
    ("https://192.168.50.2:8443/some/path", (True, "192.168.50.2", 8443)),
    ("http://192.168.50.2", (False, "192.168.50.2", 80)),
    ("192.168.50.2:8443", (True, "192.168.50.2", 8443)),
])
def test_url_parsing(url, expected):
    assert m.parse_url(url) == expected


def test_registered_and_reason_keys():
    assert SOURCES["controller"] is m.Controller
    for k in ("ok", "config", "auth_failed", "forbidden", "protocol", "timeout", "refused", "unreachable", "tls",
              "error"):
        assert "source.controller." + k in REASON_KEYS


# ================================================================== read-only
@pytest.mark.parametrize("method,path,body", [
    ("PUT", "/api/s/default/stat/sta", None),
    ("DELETE", "/api/s/default/stat/sta", None),
    ("POST", "/api/s/default/stat/sta", {"x": 1}),
    ("POST", "/proxy/network/api/s/default/stat/device", None),
    ("POST", "/api/s/default/cmd/devmgr", {"cmd": "restart"}),
    ("GET", "/api/s/default/rest/user", None),
    ("GET", "/api/s/default/stat/sta?x=1", None),
    ("GET", "/api/s/default/stat/sta/extra", None),
    ("GET", "/x/api/s/default/stat/sta", None),
    ("GET", "/api/s/de fault/stat/sta", None),
    ("GET", "/api/s/default/stat/sta", {"a": 1}),
    ("GET", "/api/login", None),
    ("POST", "/api/login", None),
    ("POST", "/api/login", {"username": "a", "password": "b", "remember": True}),
    ("POST", "/api/auth/login", {"username": "a"}),
    ("POST", "/api/logout", {"a": 1}),
    ("HEAD", "/api/s/default/stat/sta", None),
    ("get", "/api/s/default/stat/sta", None),
])
def test_forbidden_requests_are_refused_before_sending(fake, method, path, body):
    c = make(fake)
    with pytest.raises(m.ForbiddenRequest):
        c._send(method, path, body)
    assert fake.requests == [] and c.refused == 1


def test_only_reads_and_login_reach_the_controller(fake):
    c = make(fake)
    run(c.poll())
    run(c.close())
    ok, key, details = run(make(fake).test())
    assert ok
    for method, path, _, _, body in fake.requests:
        if method == "GET":
            assert re.match(r"\A(/proxy/network)?/api/s/default/stat/(sta|device)\Z", path)
            assert body is None
        else:
            assert method == "POST" and path in ("/api/auth/login", "/api/auth/logout")
            if path.endswith("login"):
                assert set(body) == {"username", "password"}
            else:
                assert body == {}
    assert fake.count("POST", "logout") == 2


# ================================================================== test() and run()
def test_test_details_and_logout(fake):
    ok, key, details = run(make(fake).test())
    assert (ok, key, details) == (True, "source.controller.ok", {"clients": 10, "devices": 5})
    assert fake.count("POST", "logout") == 1 and not fake.sessions


def test_test_failure_keys(fake):
    ok, key, details = run(m.Controller(fake.url, USER, "wrong-pw", timeout=5).test())
    assert (ok, key, details) == (False, "source.controller.auth_failed", {})
    ok, key, _ = run(m.Controller("http://127.0.0.1:1", USER, PW, timeout=2).test())
    assert (ok, key) == (False, "source.controller.refused")


def test_run_emits_topology_and_status_and_logs_in_once(fake):
    events = []

    async def go():
        c = make(fake)
        c.interval = 0.05
        task = asyncio.create_task(c.run(events.append))
        for _ in range(100):
            if sum(1 for e in events if e["type"] == "topology") >= 3:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    topo = [e for e in events if e["type"] == "topology"]
    status = [e for e in events if e["type"] == "status"]
    assert len(topo) >= 3 and topo[0]["clients"] == expected_clients()
    assert len(status) == 1 and status[0]["ok"] is True and status[0]["key"] == "source.controller.ok"
    assert status[0]["source"] == "controller"
    assert fake.count("POST", r"login\Z") == 1
    assert fake.count("POST", "logout") == 1                      # best-effort logout on cancel


def test_run_survives_unreachable_host():
    events = []

    async def go():
        c = m.Controller("http://127.0.0.1:1", USER, PW, timeout=2)
        task = asyncio.create_task(c.run(events.append))
        for _ in range(100):
            if events:
                break
            await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert events and events[0]["type"] == "status" and events[0]["ok"] is False
    assert events[0]["key"] == "source.controller.refused"


def test_run_recovers_after_session_loss(fake):
    events = []

    async def go():
        c = make(fake)
        c.interval = 0.05
        task = asyncio.create_task(c.run(events.append))
        for _ in range(100):
            if events:
                break
            await asyncio.sleep(0.05)
        fake.sessions.clear()
        n = len([e for e in events if e["type"] == "topology"])
        for _ in range(100):
            if len([e for e in events if e["type"] == "topology"]) > n + 1:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert fake.count("POST", r"login\Z") == 2
    assert [e["ok"] for e in events if e["type"] == "status"] == [True]


# ================================================================== secrets stay secret
def test_no_secret_in_repr_exceptions_events_or_logs(fake, caplog):
    secrets = [PW, CSRF, SESSION + "1", "wrong-secret-pw"]
    caplog.set_level("DEBUG")
    texts = []
    c = make(fake)
    texts += [repr(c), str(c)]
    ev = run(c.poll())
    texts.append(json.dumps(ev))
    texts += [repr(c), str(c)]
    bad = m.Controller(fake.url, USER, "wrong-secret-pw", timeout=5)
    for coro in (bad.poll(),):
        try:
            run(coro)
        except Exception as exc:
            texts += [str(exc), repr(exc), "".join(traceback.format_exception(exc))]
    fake.raw = "oops"
    c._logged_in = True
    try:
        run(c.poll())
    except Exception as exc:
        texts += [str(exc), repr(exc), "".join(traceback.format_exception(exc))]
    try:
        c._send("PUT", "/api/s/default/stat/sta", {"password": PW})
    except m.ForbiddenRequest as exc:
        texts += [str(exc), repr(exc)]
    events = []

    async def go():
        t = asyncio.create_task(make(fake).run(events.append))
        await asyncio.sleep(0.5)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    run(go())
    texts.append(json.dumps(events))
    texts.append(caplog.text)
    blob = "\n".join(texts)
    for s in secrets:
        assert s not in blob, "secret leaked"


def test_repr_shows_host_and_site_only():
    c = m.Controller("https://192.168.50.2:8443", USER, PW, site="lab")
    assert repr(c) == "Controller(host='192.168.50.2:8443', site='lab')"
    assert USER not in repr(c)
