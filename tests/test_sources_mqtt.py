"""MQTT source: fake broker on 127.0.0.1; subscribe-only proven statically and by trying; payloads never kept."""
import ast
import asyncio
import json
import os
import struct

import pytest

from lanloom.sources import mqtt as m

MQTT_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lanloom", "sources", "mqtt.py")
MARKER = b"PAYLOAD-MARKER-6612-"
USER, PASSWORD = "map", "broker-test-secret-981"


def _len(n):
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def publish(topic, payload, qos=0, retain=False, pid=7):
    """Broker -> client PUBLISH (built here in the test, never by the module)."""
    t = topic.encode()
    body = struct.pack(">H", len(t)) + t + (struct.pack(">H", pid) if qos else b"") + payload
    return bytes([0x30 | (qos << 1) | (1 if retain else 0)]) + _len(len(body)) + body


class FakeBroker:
    def __init__(self, connack_code=0, suback_code=0, password=PASSWORD):
        self.connack_code, self.suback_code, self.password = connack_code, suback_code, password
        self.packets = []           # (type, flags) of everything the client sent
        self.connect = None
        self.subscribe = None
        self.server = None
        self.writers = []
        self.sent_bytes = {}        # root -> wire bytes of new (non-retained) publishes

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        for w in self.writers:
            w.close()
        self.server.close()
        await self.server.wait_closed()

    async def read_packet(self, reader):
        first = (await reader.readexactly(1))[0]
        mult, n = 1, 0
        while True:
            b = (await reader.readexactly(1))[0]
            n += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        return first >> 4, first & 0x0F, await reader.readexactly(n)

    def parse_connect(self, body):
        assert body[:6] == b"\x00\x04MQTT" and body[6] == 4
        flags = body[7]
        keepalive = struct.unpack(">H", body[8:10])[0]
        p = 10
        fields = []
        while p < len(body):
            n = struct.unpack(">H", body[p:p + 2])[0]
            fields.append(body[p + 2:p + 2 + n].decode())
            p += 2 + n
        return {"flags": flags, "keepalive": keepalive, "fields": fields}

    def parse_subscribe(self, flags, body):
        assert flags == 2
        pid = struct.unpack(">H", body[:2])[0]
        p, topics = 2, []
        while p < len(body):
            n = struct.unpack(">H", body[p:p + 2])[0]
            topics.append((body[p + 2:p + 2 + n].decode(), body[p + 2 + n]))
            p += 3 + n
        return pid, topics

    async def handle(self, reader, writer):
        self.writers.append(writer)
        try:
            while True:
                ptype, flags, body = await self.read_packet(reader)
                self.packets.append((ptype, flags))
                if ptype == 1:
                    self.connect = self.parse_connect(body)
                    code = self.connack_code
                    if self.password and self.connect["fields"][-1:] != [self.password]:
                        code = 5
                    writer.write(bytes([0x20, 2, 0, code]))
                elif ptype == 8:
                    pid, topics = self.parse_subscribe(flags, body)
                    self.subscribe = topics
                    writer.write(bytes([0x90, 2 + len(topics)]) + struct.pack(">H", pid)
                                 + bytes([self.suback_code] * len(topics)))
                    await writer.drain()
                    await self.traffic(writer)
                elif ptype == 12:
                    writer.write(bytes([0xD0, 0]))
                elif ptype == 14:
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()

    async def traffic(self, writer):
        msgs = [
            ("home/kitchen/temp", MARKER + b"21.5", 0, True),          # retained replay: not new traffic
            ("home/kitchen/temp", MARKER + b"21.6", 0, False),
            ("home/kitchen/hum", MARKER + b"40", 1, False),             # QoS 1 with packet id
            ("home/garage", MARKER + bytes(200_000), 0, False),         # large payload, skipped unread
            ("plain", MARKER, 0, False),
            ("$SYS/broker/clients/connected", b"3", 0, False),
            ("$SYS/broker/uptime", b"86400 seconds", 0, False),
            ("$SYS/broker/version", b"9.9", 0, False),
        ]
        for topic, payload, qos, retain in msgs:
            raw = publish(topic, payload, qos, retain)
            writer.write(raw)
            if not retain:
                root = m.topic_root(topic)
                self.sent_bytes[root] = self.sent_bytes.get(root, 0) + len(raw)
        await writer.drain()


def collect(src, until, timeout=5.0):
    events = []

    async def go(broker):
        task = asyncio.create_task(src.run(events.append))
        for _ in range(int(timeout / 0.05)):
            await asyncio.sleep(0.05)
            if until(events):
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return events, go


def _totals(events):
    out = {}
    for e in events:
        if e["type"] == "mqtt":
            for root, n, b in e["topics"]:
                t = out.setdefault(root, [0, 0])
                t[0] += n
                t[1] += b
    return out


def test_run_counts_roots_bytes_and_broker_stats():
    async def main():
        broker = await FakeBroker().start()
        src = m.Mqtt("127.0.0.1", broker.port, username=USER, password=PASSWORD, sys_topics=True)
        events, go = collect(src, lambda ev: sum(e["type"] == "mqtt" for e in ev) >= 2
                             and _totals(ev).get("plain"))
        try:
            await go(broker)
        finally:
            await broker.stop()
        return broker, src, events
    broker, src, events = asyncio.run(main())
    assert broker.connect["flags"] == 0x02 | 0x80 | 0x40          # clean session + user + password
    assert broker.connect["keepalive"] == 30
    assert broker.connect["fields"][1:] == [USER, PASSWORD]
    assert broker.subscribe == [("#", 0), ("$SYS/#", 0)]
    assert {p for p, _ in broker.packets} <= {1, 8, 12, 14}
    status = [e for e in events if e["type"] == "status"]
    assert status[0]["ok"] is True and status[0]["key"] == "source.mqtt.ok"
    totals = _totals(events)
    assert totals == {root: [{"home/kitchen": 2, "home/garage": 1, "plain": 1, "$SYS": 3}[root], b]
                      for root, b in broker.sent_bytes.items()}
    assert sum(e.get("retained", 0) for e in events if e["type"] == "mqtt") == 1
    last = [e for e in events if e["type"] == "mqtt"][-1]
    assert last["broker"] == {"clients": 3, "uptime_s": 86400}
    # payloads never leave the source and are not stored anywhere on the object
    assert MARKER.decode() not in json.dumps(events)
    assert MARKER not in repr(vars(src)).encode() and MARKER.decode() not in repr(vars(src))
    assert PASSWORD not in repr(src) and PASSWORD not in json.dumps(events)


def test_pingreq_and_disconnect(monkeypatch):
    monkeypatch.setattr(m, "KEEPALIVE_S", 0.5)

    async def main():
        broker = await FakeBroker(password=None).start()
        src = m.Mqtt("127.0.0.1", broker.port)
        events, go = collect(src, lambda ev: False, timeout=1.3)
        try:
            await go(broker)
            await asyncio.sleep(0.1)
        finally:
            await broker.stop()
        return broker
    broker = asyncio.run(main())
    kinds = [p for p, _ in broker.packets]
    assert kinds[0] == 1 and kinds[1] == 8 and 12 in kinds and kinds[-1] == 14
    assert set(kinds) <= m.ALLOWED_PACKETS


@pytest.mark.parametrize("code,key", [(5, "source.mqtt.auth_failed"), (4, "source.mqtt.auth_failed"),
                                      (2, "source.mqtt.refused")])
def test_connack_refusal(code, key):
    async def main():
        broker = await FakeBroker(connack_code=code, password=None).start()
        try:
            return await m.Mqtt("127.0.0.1", broker.port).test()
        finally:
            await broker.stop()
    assert asyncio.run(main()) == (False, key, {})


def test_subscribe_denied():
    async def main():
        broker = await FakeBroker(suback_code=0x80, password=None).start()
        try:
            return await m.Mqtt("127.0.0.1", broker.port).test()
        finally:
            await broker.stop()
    assert asyncio.run(main()) == (False, "source.mqtt.subscribe_denied", {})


def test_test_details():
    async def main():
        broker = await FakeBroker().start()
        try:
            return await m.Mqtt("127.0.0.1", broker.port, username=USER, password=PASSWORD).test()
        finally:
            await broker.stop()
    ok, key, details = asyncio.run(main())
    assert (ok, key) == (True, "source.mqtt.ok")
    assert details["retained"] == 1 and details["messages"] == 7 and details["topics"] == 4


def test_run_unreachable_backoff_never_raises():
    async def main():
        broker = await FakeBroker().start()
        port = broker.port
        await broker.stop()
        src = m.Mqtt("127.0.0.1", port, timeout=1)
        events = []
        task = asyncio.create_task(src.run(events.append))
        await asyncio.sleep(0.5)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return events
    events = asyncio.run(main())
    assert events[0]["ok"] is False and events[0]["key"] in ("source.mqtt.refused", "source.mqtt.unreachable")


def test_counter_roots():
    assert m.topic_root("a/b/c/d") == "a/b"
    assert m.topic_root("a") == "a"
    assert m.topic_root("/x/y") == "/x"
    assert m.topic_root("$SYS/broker/load") == "$SYS"
    assert m.topic_root("/") == "_other"
    c = m.Counter()
    for i in range(m.MAX_ROOTS + 50):
        c.add("r%d/x" % i, 10)
    f = c.take()
    assert len(f["topics"]) == m.MAX_ROOTS + 1 and ["_other", 50, 500] in f["topics"]


@pytest.mark.parametrize("kw", [dict(host=""), dict(host="a b"), dict(host="x", port=0), dict(host="x", password="p")])
def test_config_validation(kw):
    with pytest.raises(m.MqttError) as e:
        m.Mqtt(**kw)
    assert e.value.key == "source.mqtt.config"


# ================================================================== subscribe-only: by trying
@pytest.mark.parametrize("ptype", [2, 3, 4, 5, 6, 7, 9, 10, 11, 13, 15])
def test_forbidden_packet_types_cannot_be_built(ptype):
    with pytest.raises(m.MqttError):
        m.build_packet(ptype, 0, b"\x00\x01a")


def test_wire_refuses_hand_built_publish():
    class W:
        def __init__(self):
            self.data = b""

        def write(self, b):
            self.data += b

        async def drain(self):
            pass

    wire = m._Wire(None, W())

    async def go():
        with pytest.raises(m.MqttError):
            await wire.write(publish("x", b"y"))
        with pytest.raises(m.MqttError):
            await wire.write(bytes([0x40, 2, 0, 1]))          # PUBACK
        await wire.write(m.pingreq_packet())
    asyncio.run(go())
    assert wire.writer.data == bytes([0xC0, 0])


# ================================================================== subscribe-only: statically
def test_static_no_publish_construction():
    text = open(MQTT_SRC, encoding="utf-8").read()
    tree = ast.parse(text)
    assert m.ALLOWED_PACKETS == frozenset({1, 8, 12, 14})
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "build_packet":
            arg = node.args[0]
            assert isinstance(arg, ast.Name) and arg.id in ("CONNECT", "SUBSCRIBE", "PINGREQ", "DISCONNECT", "ptype")
        if isinstance(node, ast.Name) and node.id == "PUBLISH":
            parent_ok = False
            for cmp in ast.walk(tree):
                if isinstance(cmp, ast.Compare) and any(c is node for c in ast.walk(cmp)):
                    parent_ok = True
            assigned = any(isinstance(a, ast.Assign) and any(c is node for c in ast.walk(a)) for a in ast.walk(tree))
            assert parent_ok or assigned, "PUBLISH used outside a comparison"
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            assert not 0x30 <= node.value <= 0x3F, "PUBLISH header byte constant in source"
    for word in ("publish_packet", "def publish", ".publish("):
        assert word not in text
