"""Home-automation hub source: fake WebSocket server on 127.0.0.1, allow-list proven statically and by trying."""
import ast
import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
import traceback

import pytest

from lanloom.sources import _ws
from lanloom.sources import hub as m

SOURCES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lanloom", "sources")
HUB_SRC = os.path.join(SOURCES_DIR, "hub.py")
TOKEN = "tok-" + "Z9x" * 20


# ================================================================== fake hub server
class FakeHub:
    """Minimal WebSocket server speaking the hub's auth handshake and a few read commands."""

    def __init__(self, token=TOKEN, mode=""):
        self.token, self.mode = token, mode
        self.received = []          # every message type the client sent
        self.raw = []               # every raw text message (for secret checks)
        self.server = None
        self.port = None
        self.clients = []
        self.paths = []

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        for w in self.clients:
            w.close()
        self.server.close()
        await self.server.wait_closed()

    @staticmethod
    async def read_frame(reader):
        b1, b2 = await reader.readexactly(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", await reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", await reader.readexactly(8))[0]
        assert b2 & 0x80, "client frames must be masked"
        key = await reader.readexactly(4)
        data = bytes(c ^ key[i % 4] for i, c in enumerate(await reader.readexactly(n)))
        return b1 & 0x0F, data

    @staticmethod
    def frame(opcode, data):
        n = len(data)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack("!H", n)
        else:
            head += bytes([127]) + struct.pack("!Q", n)
        return head + data

    async def send(self, writer, obj, split=False):
        data = json.dumps(obj).encode()
        if split and len(data) > 10:      # fragmented text message
            writer.write(bytes([0x01, 5]) + data[:5])
            writer.write(self.frame(0x0, data[5:]))
        else:
            writer.write(self.frame(0x1, data))
        await writer.drain()

    async def handle(self, reader, writer):
        self.clients.append(writer)
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            self.paths.append(head.split(" ")[1])
            key = next(line.split(":", 1)[1].strip() for line in head.split("\r\n")
                       if line.lower().startswith("sec-websocket-key"))
            accept = base64.b64encode(hashlib.sha1((key + _ws.GUID).encode()).digest()).decode()
            if self.mode == "bad_accept":
                accept = "x" + accept[1:]
            writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
            await self.send(writer, {"type": "auth_required", "ha_version": "1.2.3"})  # identifier: hub auth field
            while True:
                op, data = await self.read_frame(reader)
                if op == 0x8:
                    return
                if op == 0x9:
                    writer.write(self.frame(0xA, data))
                    continue
                text = data.decode()
                self.raw.append(text)
                msg = json.loads(text)
                self.received.append(msg["type"])
                await self.reply(writer, msg)
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()

    async def reply(self, writer, msg):
        t = msg["type"]
        if t == "auth":
            if msg.get("access_token") == self.token:
                await self.send(writer, {"type": "auth_ok", "ha_version": "1.2.3"})  # identifier: hub auth field
            else:
                await self.send(writer, {"type": "auth_invalid", "message": "Invalid access token"})
                writer.close()
            return
        mid = msg["id"]
        results = {
            "get_config": {"version": "2026.10.1", "location_name": "Test"},
            "get_states": [
                {"entity_id": "sensor.power", "state": "120", "attributes": {"unit_of_measurement": "W",
                                                                             "friendly_name": "Power"}},
                {"entity_id": "light.desk", "state": "off", "attributes": {"friendly_name": "Desk lamp"}},
                {"entity_id": "sun.sun", "state": "above_horizon", "attributes": {}},
            ],
            "config/device_registry/list": [
                {"id": "dev1", "name": "Meter", "name_by_user": None, "model": "M1", "manufacturer": "Acme",
                 "area_id": "office", "via_device_id": None},
                {"id": "dev2", "name": "Lamp", "model": "L2", "manufacturer": "Acme", "area_id": "office",
                 "via_device_id": "dev1"},
                {"id": "dev3", "name": "Old", "disabled_by": "user"},
            ],
            "config/entity_registry/list": [
                {"entity_id": "sensor.power", "device_id": "dev1", "name": None, "original_name": "Power"},
                {"entity_id": "light.desk", "device_id": "dev2", "name": "Desk", "area_id": None},
            ],
            "config/area_registry/list": [{"area_id": "office", "name": "Office"}],
            "subscribe_events": None,
        }
        if self.mode == "not_admin" and t.startswith("config/"):
            await self.send(writer, {"id": mid, "type": "result", "success": False,
                                     "error": {"code": "unauthorized", "message": "Unauthorized"}})
            return
        await self.send(writer, {"id": mid, "type": "result", "success": True, "result": results.get(t)},
                        split=(t == "get_states"))
        if t == "subscribe_events":
            for i, (eid, old, new) in enumerate([("light.desk", "off", "on"), ("light.desk", "on", "on"),
                                                  ("sensor.power", "120", "130"), ("light.desk", "on", "off")]):
                await self.send(writer, {"id": mid, "type": "event", "event": {
                    "event_type": "state_changed",
                    "data": {"entity_id": eid, "old_state": {"state": old}, "new_state": {
                        "state": new, "attributes": {"brightness": i}}}}})


def run(coro):
    return asyncio.run(coro)


# ================================================================== behaviour
def test_test_returns_details_and_no_token_anywhere(caplog):
    caplog.set_level(logging.DEBUG)

    async def go():
        srv = await FakeHub().start()
        try:
            h = m.Hub("http://127.0.0.1:%d" % srv.port, TOKEN)
            res = await h.test()
            return res, srv, h
        finally:
            await srv.stop()
    (ok, key, details), srv, h = run(go())
    assert (ok, key) == (True, "source.hub.ok")
    assert details == {"version": "2026.10.1", "entities": 3, "devices": 3}
    assert srv.paths == [m.WS_PATH]
    assert srv.received == ["auth", "get_config", "get_states", "config/device_registry/list"]
    assert TOKEN not in repr(h) and TOKEN not in str(h)
    assert TOKEN not in caplog.text
    assert TOKEN not in json.dumps(details)


def test_wrong_token_auth_failed():
    async def go():
        srv = await FakeHub().start()
        try:
            h = m.Hub("ws://127.0.0.1:%d/api/websocket" % srv.port, "wrong-token-value-123")
            return await h.test()
        finally:
            await srv.stop()
    assert run(go()) == (False, "source.hub.auth_failed", {})


def test_bad_accept_header_refused():
    async def go():
        srv = await FakeHub(mode="bad_accept").start()
        try:
            return await m.Hub("http://127.0.0.1:%d" % srv.port, TOKEN).test()
        finally:
            await srv.stop()
    assert run(go()) == (False, "source.hub.protocol", {})


def test_unreachable():
    async def go():
        srv = await FakeHub().start()
        port = srv.port
        await srv.stop()
        return await m.Hub("http://127.0.0.1:%d" % port, TOKEN, timeout=1).test()
    ok, key, _ = run(go())
    assert not ok and key in ("source.hub.refused", "source.hub.unreachable")


def test_run_emits_snapshot_states_and_status(caplog):
    caplog.set_level(logging.DEBUG)
    events = []

    async def go():
        srv = await FakeHub().start()
        h = m.Hub("http://127.0.0.1:%d" % srv.port, TOKEN)
        task = asyncio.create_task(h.run(events.append))
        try:
            for _ in range(100):
                await asyncio.sleep(0.05)
                if any(e["type"] == "hub_state" and e["state"] == "off" for e in events):
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await srv.stop()
        return srv, h
    srv, h = run(go())
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["hub_snapshot", "status"]
    assert events[1]["ok"] is True and events[1]["key"] == "source.hub.ok"
    snap = events[0]
    assert snap["areas"] == [{"id": "office", "name": "Office"}]
    assert [d["id"] for d in snap["devices"]] == ["dev1", "dev2"]                 # disabled device skipped
    assert snap["devices"][1] == {"id": "dev2", "name": "Lamp", "model": "L2", "manufacturer": "Acme",
                                  "area": "office", "via": "dev1", "service": False}
    ents = {e["entity_id"]: e for e in snap["entities"]}
    assert ents["sensor.power"] == {"entity_id": "sensor.power", "device_id": "dev1", "name": "Power",
                                    "state": "120", "unit": "W", "domain": "sensor", "area": "office"}
    assert ents["light.desk"]["name"] == "Desk"
    assert ents["sun.sun"]["device_id"] is None                                   # state without registry entry
    states = [(e["entity_id"], e["state"]) for e in events if e["type"] == "hub_state"]
    # attribute-only change dropped; light.desk possibly coalesced, but its latest value always arrives
    desk = [s for e, s in states if e == "light.desk"]
    assert desk[-1] == "off" and 1 <= len(desk) <= 2 and desk.count("on") <= 1
    assert ("sensor.power", "130") in states and len(states) <= 3
    assert set(srv.received) <= m.ALLOWED_TYPES
    assert "subscribe_events" in srv.received
    blob = json.dumps(events)
    assert TOKEN not in blob and TOKEN not in caplog.text
    sent_after_auth = [json.loads(r) for r in srv.raw if json.loads(r)["type"] != "auth"]
    assert all(set(x) <= {"type", "id", "event_type"} for x in sent_after_auth)


def test_not_admin_snapshot_limited():
    events = []

    async def go():
        srv = await FakeHub(mode="not_admin").start()
        h = m.Hub("http://127.0.0.1:%d" % srv.port, TOKEN)
        task = asyncio.create_task(h.run(events.append))
        try:
            for _ in range(60):
                await asyncio.sleep(0.05)
                if any(e["type"] == "hub_snapshot" for e in events):
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await srv.stop()
    run(go())
    snap = next(e for e in events if e["type"] == "hub_snapshot")
    assert snap["limited"] is True and snap["devices"] == [] and len(snap["entities"]) == 3


def test_run_reconnects_with_backoff_and_never_raises():
    events = []

    async def go():
        srv = await FakeHub(token="other").start()
        h = m.Hub("http://127.0.0.1:%d" % srv.port, TOKEN)
        task = asyncio.create_task(h.run(events.append))
        await asyncio.sleep(1.6)                 # attempts at t=0 and t=1 (backoff 1 s, then 2 s)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await srv.stop()
        return srv
    srv = run(go())
    assert events == [{"type": "status", "source": "hub", "ok": False, "key": "source.hub.auth_failed",
                       "t": events[0]["t"]}]
    assert srv.received.count("auth") == 2


def test_state_rate_limit():
    h = m.Hub("http://127.0.0.1:1", TOKEN, state_rate=20)
    for i in range(100):
        h._on_event({"event": {"event_type": "state_changed", "data": {
            "entity_id": "sensor.s%d" % i, "old_state": {"state": "1"}, "new_state": {"state": "2"}}}})
    h._on_event({"event": {"event_type": "state_changed", "data": {
        "entity_id": "sensor.s0", "old_state": {"state": "2"}, "new_state": {"state": "3"}}}})
    t0 = h._tokens_t
    first = h.take_states(t0)
    assert len(first) == 20 and first[0]["entity_id"] == "sensor.s0" and first[0]["state"] == "3"
    assert h.take_states(t0 + 0.01) == []
    assert len(h.take_states(t0 + 0.5)) == 10
    assert len(h.take_states(t0 + 10)) == 20                 # bucket never exceeds one second of budget
    assert len(h.take_states(t0 + 20)) == 20
    assert len(h.take_states(t0 + 30)) == 20
    assert len(h.take_states(t0 + 40)) == 10                 # 100 distinct entities in total, s0 only once
    assert h.take_states(t0 + 50) == []


@pytest.mark.parametrize("url,expected", [
    ("http://192.168.1.10:8123", "ws://192.168.1.10:8123/api/websocket"),
    ("https://hub.example.org", "wss://hub.example.org/api/websocket"),
    ("192.168.1.10:8123", "ws://192.168.1.10:8123/api/websocket"),
    ("wss://hub.example.org/custom/ws", "wss://hub.example.org/custom/ws"),
    ("http://[fd00::5]:8123/", "ws://[fd00::5]:8123/api/websocket"),
])
def test_hub_ws_url(url, expected):
    assert m.hub_ws_url(url) == expected


@pytest.mark.parametrize("url", ["", "ftp://x", "http://user:pw@192.168.1.10"])
def test_hub_ws_url_invalid(url):
    with pytest.raises(m.SourceError):
        m.hub_ws_url(url)


@pytest.mark.parametrize("tok", ["", " ", "a b", "x" * 9000, None, 12345])
def test_token_validation(tok):
    with pytest.raises(m.SourceError) as e:
        m.Hub("http://192.168.1.10", tok)
    assert "x" * 50 not in str(e.value)


# ================================================================== allow-list: by trying
class RecordingWs:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


@pytest.mark.parametrize("type_,fields", [
    ("call" + "_service", {"domain": "light", "service": "turn_on"}),
    ("execute_script", {}), ("fire_event", {"event_type": "x"}), ("subscribe_trigger", {}),
    ("config/entity_registry/update", {}), ("unsubscribe_events", {"subscription": 1}),
    ("subscribe_events", {}), ("subscribe_events", {"event_type": "call" + "_service"}),
    ("subscribe_events", {"event_type": None}), ("get_states", {"extra": 1}),
    ("auth", {"access_token": "x", "extra": 1}), ("ping", {}), ("render_template", {"template": "x"}),
])
def test_forbidden_messages_are_refused_and_nothing_is_sent(type_, fields):
    h = m.Hub("http://127.0.0.1:1", TOKEN)
    conn = m._Conn(RecordingWs())

    async def go():
        with pytest.raises(m.ForbiddenMessage):
            await h._send(conn, type_, **fields)
    asyncio.run(go())
    assert conn.ws.sent == [] and h.refused == 1 and conn.pending == {}


def test_allowed_messages_go_out_with_fixed_fields():
    h = m.Hub("http://127.0.0.1:1", TOKEN)
    conn = m._Conn(RecordingWs())

    async def go():
        await h._send(conn, "auth", access_token="abc")
        for t in sorted(m.ALLOWED_TYPES - {"auth", "subscribe_events"}):
            await h._send(conn, t)
        await h._send(conn, "subscribe_events", event_type="state_changed")
    asyncio.run(go())
    msgs = [json.loads(s) for s in conn.ws.sent]
    assert msgs[0] == {"access_token": "abc", "type": "auth"}
    assert [x["type"] for x in msgs[1:]] == sorted(m.ALLOWED_TYPES - {"auth", "subscribe_events"}) + ["subscribe_events"]
    assert [x["id"] for x in msgs[1:]] == list(range(1, len(msgs)))


# ================================================================== allow-list: statically
def _tree():
    return ast.parse(open(HUB_SRC, encoding="utf-8").read())


def test_source_has_no_service_call_text():
    text = open(HUB_SRC, encoding="utf-8").read()
    for word in ("call" + "_service", "fire_event", "execute_script", "unsubscribe", "render_template"):
        assert word not in text


def test_allow_list_is_exactly_the_contract():
    assert m.ALLOWED_TYPES == frozenset({"auth", "get_states", "get_config", "config/device_registry/list",
                                         "config/entity_registry/list", "config/area_registry/list",
                                         "subscribe_events"})
    assert m.ALLOWED_EVENTS == frozenset({"state_changed"})


def test_send_text_only_inside_the_single_send_function():
    tree = _tree()
    owners = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and node.attr in ("send_text", "_frame", "write", "sendall"):
                    owners.append((fn.name, node.attr))
    assert owners == [("_send", "send_text")], owners


def test_every_send_site_uses_an_allowed_literal_type():
    tree = _tree()
    sites = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("_send", "_request", "_registry"):
            sites += 1
            assert len(node.args) >= 2, ast.dump(node)
            arg = node.args[1]
            if isinstance(arg, ast.Name) and arg.id == "type_":
                continue                     # pass-through inside _request/_registry, checked by _send
            assert isinstance(arg, ast.Constant) and arg.value in m.ALLOWED_TYPES, ast.dump(node)
            for kw in node.keywords:
                assert kw.arg in m.ALLOWED_FIELDS.get(arg.value, ()), ast.dump(node)
                if kw.arg == "event_type":
                    assert isinstance(kw.value, ast.Constant) and kw.value.value in m.ALLOWED_EVENTS
    assert sites >= 8


def test_websocket_client_only_used_by_hub():
    for name in os.listdir(SOURCES_DIR):
        if name.endswith(".py") and name not in ("hub.py", "_ws.py"):
            text = open(os.path.join(SOURCES_DIR, name), encoding="utf-8").read()
            assert "_ws" not in text, name


def test_ws_path_line_carries_identifier_marker():
    for line in open(HUB_SRC, encoding="utf-8"):
        if "/api/websocket" in line or "ha_version" in line:  # identifier: hub path and auth field
            assert "identifier" in line, line


def test_exceptions_never_contain_token():
    async def go():
        srv = await FakeHub(token="other").start()
        try:
            h = m.Hub("http://127.0.0.1:%d" % srv.port, TOKEN)
            try:
                conn = await h._open()
                await conn.close()
            except m.SourceError as exc:
                return exc
        finally:
            await srv.stop()
    exc = run(go())
    assert exc is not None and exc.key == "source.hub.auth_failed"
    assert TOKEN not in "".join(traceback.format_exception(exc))
