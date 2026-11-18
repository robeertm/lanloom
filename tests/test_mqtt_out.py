"""MQTT output (H5): opt-in sensors for a home-automation hub through MQTT discovery.

* The client builds only CONNECT, PUBLISH, PINGREQ and DISCONNECT (never SUBSCRIBE) and publishes only to the topics
  computed for its own node - checked when a packet is built and again right before it is written (AST + behaviour).
* Against a stand-in broker: discovery configs for every entity, values, availability with last will, only changes
  are sent, switching off removes everything again, a refused password is reported.
* Privacy: only counts, a level, a flag and a time - never a device name, address or person from the summary.
"""
import ast
import asyncio
import json
import struct
import time
from pathlib import Path

import pytest

from lanloom import mqtt_out
from lanloom.mqtt_out import (ALLOWED_PACKETS, DEFAULTS, OBJECTS, MqttOut, OutError, Publisher, Topics, addon_broker,
                              discovery, publish_packet, snapshot, validate)

SRC = Path(mqtt_out.__file__)


# ------------------------------------------------------------------ source checks
def test_only_allow_listed_packet_types_exist():
    assert ALLOWED_PACKETS == {1, 3, 12, 14}                 # CONNECT, PUBLISH, PINGREQ, DISCONNECT
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "SUBSCRIBE" not in names and "UNSUBSCRIBE" not in names
    built = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "build_packet":
            built.append(node.args[0].id)
    assert built and set(built) <= {"CONNECT", "PUBLISH", "PINGREQ", "DISCONNECT"}, built


def test_raw_writes_only_in_the_checked_write():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    sites = []

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, stack + [child.name])
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and \
                    child.func.attr in ("write", "send", "sendall", "writelines"):
                sites.append((".".join(stack), child.func.attr, ast.unparse(child.func.value)))
            walk(child, stack)
    walk(tree, [])
    raw = [s for s in sites if s[2] == "self.writer"]
    assert raw == [("Publisher.write", "write", "self.writer")], sites


def test_build_packet_refuses_other_types():
    with pytest.raises(OutError):
        mqtt_out.build_packet(8, 2, b"\x00\x01\x00\x01#\x00")          # SUBSCRIBE "#"


# ------------------------------------------------------------------ topics and settings
def test_topics_of_one_node():
    t = Topics(dict(DEFAULTS))
    assert t.status == "lanloom/lanloom/status"
    assert t.state["devices_online"] == "lanloom/lanloom/devices_online"
    assert t.config["internet"] == "%s/binary_sensor/lanloom/internet/config" % mqtt_out.DEFAULT_DISCOVERY
    assert t.config["health"] == "%s/sensor/lanloom/health/config" % mqtt_out.DEFAULT_DISCOVERY
    assert len(t.allowed) == 1 + 2 * len(OBJECTS)
    for bad in ("lanloom/other/status", "otherapp/bridge/request/permit_join", "lanloom/lanloom/#", "$SYS/x",
                "%s/switch/lanloom/x/config" % mqtt_out.DEFAULT_DISCOVERY, "lanloom/lanloom/devices_online/set"):
        with pytest.raises(OutError):
            t.check(bad)
        with pytest.raises(OutError):
            publish_packet(t, bad, b"1")


def test_write_checks_the_topic_again():
    pub = Publisher("127.0.0.1", 1, Topics(dict(DEFAULTS)))
    forged = bytes([0x31]) + mqtt_out._length(len(mqtt_out._str("other/topic")) + 1) + mqtt_out._str("other/topic") + b"1"
    with pytest.raises(OutError):
        asyncio.run(pub.write(forged))
    with pytest.raises(OutError):
        asyncio.run(pub.write(bytes([0x82, 0x00])))                     # SUBSCRIBE type in the first byte


@pytest.mark.parametrize("patch,key", [
    ({"discovery_prefix": "a/+/b"}, "mqtt_out.err.topic"), ({"base_topic": "#"}, "mqtt_out.err.topic"),
    ({"base_topic": "a//b"}, "mqtt_out.err.topic"), ({"node_id": "Bad Node"}, "mqtt_out.err.node"),
    ({"port": 0}, "mqtt_out.err.port"), ({"port": True}, "mqtt_out.err.port"), ({"host": "a b"}, "mqtt_out.err.host"),
    ({"enabled": True}, "mqtt_out.err.host"), ({"broker": "cloud"}, "mqtt_out.err.value"),
    ({"enabled": "yes"}, "mqtt_out.err.value"), ({"base_topic": "x", "discovery_prefix": "x"}, "mqtt_out.err.topic"),
])
def test_validation(patch, key):
    with pytest.raises(ValueError) as exc:
        validate({}, patch)
    assert str(exc.value) == key


def test_validation_accepts_a_good_patch():
    out = validate({}, {"enabled": True, "host": "192.0.2.10", "port": "8883", "tls": True, "node_id": "lanloom_2",
                        "base_topic": "home/lanloom", "discovery_prefix": "disc"})
    assert out["port"] == 8883 and out["node_id"] == "lanloom_2" and out["base_topic"] == "home/lanloom"


# ------------------------------------------------------------------ figures and privacy
class FakeOverview:
    def __init__(self):
        self.data = {
            "devices": {"total": 12, "online": 10, "offline": 2, "new": 1, "unsure": 0, "hidden": 0},
            "wan": {"state": "up", "router": "dev-1"},
            "report": {"available": True, "latest": {"day": "2026-11-18", "created": 1763445600.0, "open": 3, "level": "warn",
                                                     "points": 4, "read": False}},
            "health": {"level": "warn"},
            "notes": [{"id": "dev:1", "level": "warn", "vars": {"name": "Living room camera"}},
                      {"id": "x", "level": "info", "vars": {"name": "Alex's phone"}}],
            "new": [{"id": "d9", "ip": "192.168.20.44", "title": "Alex's phone"}],
        }

    def summary(self, now=None):
        return self.data


class FakeVulns:
    def open_findings(self, now=None):
        return [("d1", {}), ("d2", {})]


class FakeStore:
    def __init__(self):
        self.s = {}

    def get_setting(self, k, default=None):
        return self.s.get(k, default)

    def set_setting(self, k, v):
        self.s[k] = v


class FakeVault:
    def __init__(self):
        self.v = {}

    def put(self, k, v):
        self.v[k] = v

    def get(self, k):
        return self.v.get(k)

    def has(self, k):
        return k in self.v

    def delete(self, k):
        self.v.pop(k, None)


class FakeNotify:
    def __init__(self):
        self.events = []

    def emit(self, *a, **kw):
        self.events.append((a, kw))


class FakeCore:
    def __init__(self):
        self.overview = FakeOverview()
        self.vulns = FakeVulns()
        self.notify = FakeNotify()


class Cfg:
    demo = False
    addon = False


def test_snapshot_carries_only_figures():
    snap = snapshot(FakeCore(), 0, 2)
    assert set(snap) == set(OBJECTS)
    assert snap == {"devices_total": "12", "devices_online": "10", "devices_offline": "2", "devices_new": "1",
                    "internet": "ON", "open_issues": "1", "health": "warn", "vulnerability_hints": "2",
                    "nightly_report": "2025-11-18T06:00:00+00:00", "nightly_open_points": "3"}
    text = json.dumps(snap)
    for private in ("Alex", "camera", "192.168", "dev-1", "d9"):
        assert private not in text


def test_unknown_values_are_none_for_the_hub():
    core = FakeCore()
    core.overview.data["wan"] = {"state": "unknown"}
    core.overview.data["report"] = {"available": False}
    core.overview.data["health"] = {}
    snap = snapshot(core, 0, None)
    assert snap["internet"] == "None" and snap["nightly_report"] == "None" and snap["nightly_open_points"] == "None"
    assert snap["health"] == "None" and snap["vulnerability_hints"] == "None"
    core.overview.data["wan"] = {"state": "down"}
    assert snapshot(core, 0, 0)["internet"] == "OFF"


def test_discovery_payloads():
    t = Topics(dict(DEFAULTS))
    cfgs = discovery(t, {"devices_online": "Devices online"})
    assert set(cfgs) == set(t.config.values())
    c = cfgs[t.config["devices_online"]]
    assert c["name"] == "Devices online" and c["unique_id"] == "lanloom_lanloom_devices_online"
    assert c["state_topic"] == t.state["devices_online"] and c["availability_topic"] == t.status
    assert c["device"]["identifiers"] == ["lanloom_lanloom"] and c["device"]["name"] == "Lanloom"
    assert "value_template" not in c and "command_topic" not in json.dumps(cfgs)          # plain values, nothing to command
    b = cfgs[t.config["internet"]]
    assert b["device_class"] == "connectivity" and b["payload_on"] == "ON"
    ts = cfgs[t.config["nightly_report"]]
    assert ts["device_class"] == "timestamp"


def test_entity_names_follow_the_instance_language():
    st = FakeStore()
    assert mqtt_out.entity_names(st)["devices_online"] == "Devices online"
    st.set_setting("language", "de")
    assert mqtt_out.entity_names(st)["devices_online"] == "Geräte online"


# ------------------------------------------------------------------ stand-in broker
class Broker:
    """Minimal MQTT 3.1.1 broker stand-in: answers CONNECT and PINGREQ, records everything it receives."""

    def __init__(self, refuse_code=0):
        self.refuse_code = refuse_code
        self.connects, self.messages, self.types = [], [], []
        self.server = None
        self.retained = {}

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        try:
            while True:
                first = (await reader.readexactly(1))[0]
                n, mult = 0, 1
                while True:
                    b = (await reader.readexactly(1))[0]
                    n += (b & 0x7F) * mult
                    if not b & 0x80:
                        break
                    mult *= 128
                body = await reader.readexactly(n) if n else b""
                ptype = first >> 4
                self.types.append(ptype)
                if ptype == 1:
                    self.connects.append(self.parse_connect(body))
                    writer.write(bytes([0x20, 2, 0, self.refuse_code]))
                    await writer.drain()
                    if self.refuse_code:
                        break
                elif ptype == 3:
                    tlen = struct.unpack(">H", body[:2])[0]
                    topic = body[2:2 + tlen].decode()
                    payload = body[2 + tlen:]
                    self.messages.append((topic, payload, bool(first & 1)))
                    if first & 1:
                        if payload:
                            self.retained[topic] = payload
                        else:
                            self.retained.pop(topic, None)
                elif ptype == 12:
                    writer.write(bytes([0xD0, 0]))
                    await writer.drain()
                elif ptype == 14:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    @staticmethod
    def parse_connect(body):
        def s(i):
            ln = struct.unpack(">H", body[i:i + 2])[0]
            return body[i + 2:i + 2 + ln], i + 2 + ln
        _, i = s(0)
        flags = body[i + 1]
        i += 4
        cid, i = s(i)
        out = {"flags": flags, "client_id": cid.decode()}
        if flags & 0x04:
            wt, i = s(i)
            wm, i = s(i)
            out["will"] = (wt.decode(), wm, bool(flags & 0x20))
        if flags & 0x80:
            u, i = s(i)
            out["username"] = u.decode()
        if flags & 0x40:
            p, i = s(i)
            out["password"] = p.decode()
        return out


async def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


def make_out(port, **extra):
    store, vault, core = FakeStore(), FakeVault(), FakeCore()
    out = MqttOut(core, store, vault, Cfg())
    core.mqtt_out = out
    return out, store, vault, core


def test_publishes_discovery_values_and_availability():
    async def go():
        broker = Broker()
        port = await broker.start()
        out, store, vault, core = make_out(port)
        task = asyncio.ensure_future(out.run())
        await asyncio.sleep(0.05)
        out.update({"enabled": True, "host": "127.0.0.1", "port": port, "username": "lanloom", "password": "s3cret-pw"})
        t = Topics(out.settings())
        assert await wait_for(lambda: t.state["nightly_open_points"] in broker.retained)
        assert out.status["state"] == "connected"
        conn = broker.connects[0]
        assert conn["username"] == "lanloom" and conn["password"] == "s3cret-pw"
        assert conn["will"] == (t.status, b"offline", True)
        assert broker.retained[t.status] == b"online"
        for obj in OBJECTS:
            cfg = json.loads(broker.retained[t.config[obj]])
            assert cfg["state_topic"] == t.state[obj]
        assert broker.retained[t.state["devices_online"]] == b"10"
        assert broker.retained[t.state["internet"]] == b"ON"
        assert all(topic in t.allowed for topic, _, _ in broker.messages)
        assert all(retain for _, _, retain in broker.messages)
        assert set(broker.types) <= {1, 3, 12, 14}                         # never SUBSCRIBE
        everything = b" ".join(p for _, p, _ in broker.messages)
        for private in (b"Alex", b"camera", b"192.168", b"s3cret"):
            assert private not in everything
        # only changes are sent
        sent = len(broker.messages)
        core.overview.data["devices"]["online"] = 9
        out._wake.set()
        assert await wait_for(lambda: broker.retained.get(t.state["devices_online"]) == b"9")
        new = broker.messages[sent:]
        assert [m[0] for m in new] == [t.state["devices_online"]], new
        # switching off removes the entities and values again (empty retained messages)
        out.update({"enabled": False})
        assert await wait_for(lambda: not broker.retained)
        assert out.status["state"] == "off"
        task.cancel()
        await broker.stop()
        return vault
    vault = asyncio.run(go())
    assert vault.get(mqtt_out.SECRET) == "s3cret-pw"


def test_stopping_lanloom_leaves_offline_behind():
    async def go():
        broker = Broker()
        port = await broker.start()
        out, *_ = make_out(port)
        task = asyncio.ensure_future(out.run())
        await asyncio.sleep(0.05)
        out.update({"enabled": True, "host": "127.0.0.1", "port": port})
        t = Topics(out.settings())
        assert await wait_for(lambda: broker.retained.get(t.status) == b"online")
        task.cancel()                                   # Lanloom shuts down
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert await wait_for(lambda: broker.retained.get(t.status) == b"offline")
        assert broker.types[-1] == 14                   # then a clean DISCONNECT
        await broker.stop()
    asyncio.run(go())


def test_renaming_the_node_removes_the_old_entities():
    async def go():
        broker = Broker()
        port = await broker.start()
        out, *_ = make_out(port)
        task = asyncio.ensure_future(out.run())
        await asyncio.sleep(0.05)
        out.update({"enabled": True, "host": "127.0.0.1", "port": port})
        old = Topics(out.settings())
        assert await wait_for(lambda: old.state["nightly_open_points"] in broker.retained)
        out.update({"node_id": "second"})
        new = Topics(out.settings())
        assert await wait_for(lambda: new.state["nightly_open_points"] in broker.retained)
        assert await wait_for(lambda: not any(k in broker.retained for k in old.allowed))
        task.cancel()
        await broker.stop()
    asyncio.run(go())


def test_refused_password_is_reported():
    async def go():
        broker = Broker(refuse_code=5)
        port = await broker.start()
        out, *_ = make_out(port)
        res = await out.test({"host": "127.0.0.1", "port": port, "password": "wrong"})
        assert res == {"ok": False, "error": "mqtt_out.err.auth"}
        task = asyncio.ensure_future(out.run())
        await asyncio.sleep(0.05)
        out.update({"enabled": True, "host": "127.0.0.1", "port": port})
        assert await wait_for(lambda: out.status["state"] == "failed")
        assert out.status["error"] == "mqtt_out.err.auth"
        assert not broker.messages                                           # nothing published without CONNACK 0
        task.cancel()
        await broker.stop()
    asyncio.run(go())


def test_test_connection_publishes_nothing():
    async def go():
        broker = Broker()
        port = await broker.start()
        out, *_ = make_out(port)
        res = await out.test({"host": "127.0.0.1", "port": port})
        await asyncio.sleep(0.05)
        assert res == {"ok": True} and not broker.messages and broker.types == [1, 14]
        res = await out.test({"host": "127.0.0.1", "port": 1})
        assert res["ok"] is False and res["error"] == "mqtt_out.err.unreachable"
        await broker.stop()
    asyncio.run(go())


def test_long_failure_becomes_a_note_and_an_event():
    out, store, vault, core = make_out(1)
    clock = [1000.0]
    out.clock = lambda: clock[0]
    out._fail("mqtt_out.err.unreachable")
    assert mqtt_out.overview_note(core, clock[0]) == [] and not core.notify.events
    clock[0] += mqtt_out.FAIL_NOTE_S + 1
    out._fail("mqtt_out.err.unreachable")
    note = mqtt_out.overview_note(core, clock[0])
    assert note and note[0]["key"] == "ov.note.mqtt_out_failed" and note[0]["vars"]["reason_key"] == "mqtt_out.err.unreachable"
    assert core.notify.events[0][0][0] == "mqtt_out_down"
    out._set("connected")
    out._recovered()
    assert core.notify.events[-1][1].get("resolve") is True
    assert mqtt_out.overview_note(core, clock[0]) == []


# ------------------------------------------------------------------ broker of the add-on system
class FakeResp:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        return json.dumps(self.data).encode()


def test_addon_broker_lookup():
    seen = []

    def opener(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization")))
        return FakeResp({"result": "ok", "data": {"host": "core-broker", "port": 1883, "ssl": False,
                                                  "username": "addons", "password": "pw", "protocol": "3.1.1"}})
    b = addon_broker(token="tok", opener=opener)
    assert b == {"host": "core-broker", "port": 1883, "tls": False, "username": "addons", "password": "pw"}
    assert seen == [("http://supervisor/services/mqtt", "Bearer tok")]
    with pytest.raises(OutError) as exc:
        addon_broker(token="")
    assert exc.value.key == "mqtt_out.err.no_addon_broker"
    with pytest.raises(OutError):
        addon_broker(token="tok", opener=lambda req, timeout: FakeResp({"result": "error", "data": {}}))


def test_addon_broker_choice_needs_addon_mode(monkeypatch):
    out, *_ = make_out(1)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    with pytest.raises(ValueError) as exc:
        out.update({"enabled": True, "broker": "addon"})
    assert str(exc.value) == "mqtt_out.err.no_addon_broker"
    out.cfg = type("C", (), {"demo": False, "addon": True})()
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    v = out.update({"enabled": True, "broker": "addon"})
    assert v["settings"]["broker"] == "addon" and v["addon_broker"] is True


# ------------------------------------------------------------------ API
@pytest.fixture()
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from lanloom import config as config_mod
    from lanloom.main import create_app
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        code = (tmp_path / "setup-code.txt").read_text().strip()
        r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"},
                   headers={"X-Lanloom": "1"})
        assert r.status_code == 200
        yield c


def test_api_view_change_and_audit(api):
    hdr = {"X-Lanloom": "1"}
    v = api.get("/api/mqtt-out").json()
    assert v["settings"]["enabled"] is False and v["status"]["state"] == "off" and len(v["entities"]) == len(OBJECTS)
    r = api.put("/api/mqtt-out", json={"enabled": True, "host": "192.0.2.10", "password": "pw-123456"}, headers=hdr)
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["settings"]["enabled"] and v["password_set"] and "password" not in json.dumps(v["settings"])
    assert v["status"]["state"] == "connected" and v["status"]["demo"]               # the demo never connects
    assert "pw-123456" not in json.dumps(v)
    bad = api.put("/api/mqtt-out", json={"node_id": "NO SPACES"}, headers=hdr)
    assert bad.status_code == 400 and bad.json()["error"] == "mqtt_out.err.node"
    assert api.post("/api/mqtt-out/test", json={}, headers=hdr).json()["ok"] is True
    log = api.get("/api/audit").json()
    actions = [e["action"] for e in log.get("entries", log if isinstance(log, list) else [])]
    assert "mqtt_out.change" in actions and "mqtt_out.test" in actions
    assert "pw-123456" not in json.dumps(log)
