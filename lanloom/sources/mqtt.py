"""Read-only MQTT 3.1.1 source: message and byte counts per topic root, never payloads.

The client sends only CONNECT (clean session, keepalive 30 s), SUBSCRIBE ("#" at QoS 0, optionally "$SYS/#"),
PINGREQ and DISCONNECT - the allow-list ``ALLOWED_PACKETS`` is checked when a packet is built and again right
before it is written. There is no code path that can build a PUBLISH packet.

For every PUBLISH received only the topic name and the packet length are looked at; the payload is read off the
socket in chunks and discarded without being stored. The only exception are a few broker statistics under
``$SYS/`` (``SYS_STATS``, when enabled): short, purely numeric values of the broker itself.

Every second: ``{"type": "mqtt", "t", "dt", "topics": [[root, messages, bytes]], "retained": n}`` where root is
the first two topic levels. Retained messages replayed on subscribe are not new traffic and only counted apart.
"""
from __future__ import annotations

import asyncio
import os
import ssl
import struct
import time

from ._common import SourceError, StatusTracker, clip, emit_event, now, run_with_backoff, short_test

MODULE = "mqtt"
SOURCE = "mqtt"

CONNECT, CONNACK, PUBLISH, SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 8, 9, 12, 13, 14
ALLOWED_PACKETS = frozenset({CONNECT, SUBSCRIBE, PINGREQ, DISCONNECT})

KEEPALIVE_S = 30
MAX_REMAINING = 268435455          # protocol maximum
MAX_TOPIC = 65535
MAX_ROOTS = 500
OTHER_ROOT = "_other"
SYS_ROOT = "$SYS"
SYS_STATS = {                       # $SYS topic -> stats field (numeric values only, <= 32 bytes)
    "$SYS/broker/clients/connected": "clients",
    "$SYS/broker/subscriptions/count": "subscriptions",
    "$SYS/broker/messages/received": "messages_received",
    "$SYS/broker/messages/sent": "messages_sent",
    "$SYS/broker/load/bytes/received": "bytes_received",
    "$SYS/broker/load/bytes/sent": "bytes_sent",
    "$SYS/broker/uptime": "uptime_s",
}
_CHUNK = 65536


class MqttError(SourceError):
    pass


def _length(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    if len(b) > MAX_TOPIC:
        raise MqttError("source.mqtt.config", "string too long")
    return struct.pack(">H", len(b)) + b


def build_packet(ptype: int, flags: int, body: bytes) -> bytes:
    """Build one packet - only types on the allow-list."""
    if ptype not in ALLOWED_PACKETS:
        raise MqttError("source.mqtt.forbidden", "packet type not allowed (read-only)")
    return bytes([(ptype << 4) | (flags & 0x0F)]) + _length(len(body)) + body


def connect_packet(client_id: str, username: str | None, password: str | None, keepalive: int = KEEPALIVE_S) -> bytes:
    flags = 0x02 | (0x80 if username else 0) | (0x40 if password else 0)      # clean session
    head = _str("MQTT") + bytes([4, flags]) + struct.pack(">H", keepalive)
    body = _str(client_id) + (_str(username) if username else b"") + (_str(password) if password else b"")
    return build_packet(CONNECT, 0, head + body)


def subscribe_packet(packet_id: int, topics) -> bytes:
    return build_packet(SUBSCRIBE, 0x02, struct.pack(">H", packet_id) + b"".join(_str(t) + b"\x00" for t in topics))


def pingreq_packet() -> bytes:
    return build_packet(PINGREQ, 0, b"")


def disconnect_packet() -> bytes:
    return build_packet(DISCONNECT, 0, b"")


def topic_root(topic: str, levels: int = 2) -> str:
    """Topic -> root (first ``levels`` levels). Only the topic name is used, never the payload."""
    if topic.startswith("$"):
        return SYS_ROOT
    parts = topic.split("/")[:levels]
    root = "/".join(parts)
    if not root.strip("/"):
        return OTHER_ROOT
    return clip(root, 128) or OTHER_ROOT


class Counter:
    """Sums per root since the last ``take()``; numbers only."""

    def __init__(self, levels: int = 2):
        self.levels = levels
        self.open: dict[str, list[int]] = {}
        self.retained = 0
        self.since = time.monotonic()
        self.total = 0

    def add(self, topic: str, wire_bytes: int, retained: bool = False) -> None:
        if retained:
            self.retained += 1
            return
        root = topic_root(topic, self.levels)
        if root not in self.open and len(self.open) >= MAX_ROOTS:
            root = OTHER_ROOT
        e = self.open.setdefault(root, [0, 0])
        e[0] += 1
        e[1] += int(wire_bytes)
        self.total += 1

    def take(self, mono: float | None = None) -> dict:
        mono = time.monotonic() if mono is None else mono
        frame = {"type": "mqtt", "source": SOURCE, "t": now(), "dt": round(max(0.0, mono - self.since), 3),
                 "topics": [[k, v[0], v[1]] for k, v in sorted(self.open.items(), key=lambda kv: -kv[1][1])],
                 "retained": self.retained}
        self.open, self.retained, self.since = {}, 0, mono
        return frame


class _Wire:
    """Packet reader/writer on an asyncio stream. ``write`` re-checks the allow-list on the raw first byte."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.sent: dict[int, int] = {}
        self.last_rx = time.monotonic()

    async def write(self, raw: bytes) -> None:
        ptype = raw[0] >> 4
        if ptype not in ALLOWED_PACKETS:           # belt and braces: a hand-built packet cannot leave either
            raise MqttError("source.mqtt.forbidden", "packet type not allowed")
        self.writer.write(raw)
        await self.writer.drain()
        self.sent[ptype] = self.sent.get(ptype, 0) + 1

    async def _exact(self, n: int) -> bytes:
        try:
            return await self.reader.readexactly(n)
        except asyncio.IncompleteReadError:
            raise MqttError("source.mqtt.closed", "connection closed") from None

    async def _skip(self, n: int) -> None:
        while n > 0:
            k = min(n, _CHUNK)
            await self._exact(k)               # read and drop: payload bytes are never kept
            n -= k

    async def read_header(self) -> tuple[int, int, int, int]:
        """-> (type, flags, remaining_length, header_length)."""
        first = (await self._exact(1))[0]
        mult, n = 1, 0
        for i in range(4):
            b = (await self._exact(1))[0]
            n += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        else:
            raise MqttError("source.mqtt.protocol", "invalid remaining length")
        self.last_rx = time.monotonic()
        return first >> 4, first & 0x0F, n, 2 + i

    async def read_small(self, n: int, limit: int = 4096) -> bytes:
        if n > limit:
            raise MqttError("source.mqtt.protocol", "unexpected packet size")
        return await self._exact(n) if n else b""

    async def read_publish(self, flags: int, remaining: int, want_payload_max: int = 0):
        """PUBLISH body -> (topic, payload_or_None). The payload is only returned when ``want_payload_max`` is
        large enough (used for numeric broker statistics); otherwise it is skipped unread."""
        if remaining < 2:
            raise MqttError("source.mqtt.protocol", "PUBLISH too short")
        tlen = struct.unpack(">H", await self._exact(2))[0]
        rest = remaining - 2
        if tlen > rest:
            raise MqttError("source.mqtt.protocol", "PUBLISH topic too long")
        topic = (await self._exact(tlen)).decode("utf-8", "replace")
        rest -= tlen
        qos = (flags >> 1) & 3
        if qos:
            if rest < 2:
                raise MqttError("source.mqtt.protocol", "PUBLISH without packet id")
            await self._exact(2)
            rest -= 2
        if want_payload_max and rest <= want_payload_max:
            return topic, await self._exact(rest)
        await self._skip(rest)
        return topic, None


def _sys_value(payload: bytes | None):
    if payload is None or len(payload) > 32:
        return None
    text = payload.decode("ascii", "replace").strip().split(" ")[0]
    if text.isdigit():
        return int(text)
    return None


class Mqtt:
    """Live source: MQTT broker, subscribe-only."""

    name = SOURCE

    def __init__(self, host, port=1883, username=None, password=None, tls=False, allow_self_signed=False,
                 sys_topics=False, client_id=None, interval=1.0, levels=2, timeout=10.0):
        self.host = str(host or "").strip()
        if not self.host or len(self.host) > 253 or any(c.isspace() for c in self.host):
            raise MqttError("source.mqtt.config", "host missing or invalid")
        self.port = int(port)
        if not 0 < self.port < 65536:
            raise MqttError("source.mqtt.config", "port invalid")
        self._username = str(username) if username else None
        self._password = str(password) if password else None
        if self._password and not self._username:
            raise MqttError("source.mqtt.config", "password requires a user name")
        self.tls = bool(tls)
        self.allow_self_signed = bool(allow_self_signed)
        self.sys_topics = bool(sys_topics)
        self.client_id = clip(client_id, 23) if client_id else "lanloom-" + os.urandom(4).hex()
        self.interval = max(0.2, float(interval))
        self.timeout = max(1.0, float(timeout))
        self.counter = Counter(levels)
        self.broker: dict[str, int] = {}
        self.sent: dict[int, int] = {}

    def __repr__(self):
        return "Mqtt(host=%r, port=%d, tls=%r)" % (self.host, self.port, self.tls)

    __str__ = __repr__

    def _ssl(self):
        if not self.tls:
            return None
        ctx = ssl.create_default_context()
        if self.allow_self_signed:             # explicit opt-in only
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _connect(self) -> _Wire:
        ctx = self._ssl()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=ctx, server_hostname=self.host if ctx else None),
            self.timeout)
        wire = _Wire(reader, writer)
        try:
            await wire.write(connect_packet(self.client_id, self._username, self._password))
            ptype, _, n, _ = await asyncio.wait_for(wire.read_header(), self.timeout)
            body = await wire.read_small(n)
            if ptype != CONNACK or len(body) < 2:
                raise MqttError("source.mqtt.protocol", "no CONNACK")
            if body[1] in (4, 5):
                raise MqttError("source.mqtt.auth_failed", "broker refused credentials")
            if body[1] != 0:
                raise MqttError("source.mqtt.refused", "broker refused connection")
            topics = ["#"] + (["$SYS/#"] if self.sys_topics else [])
            await wire.write(subscribe_packet(1, topics))
        except BaseException:
            writer.close()
            raise
        return wire

    async def _close(self, wire: _Wire) -> None:
        try:
            await asyncio.wait_for(wire.write(disconnect_packet()), 2)
        except (OSError, SourceError, asyncio.TimeoutError):
            pass
        try:
            wire.writer.close()
        except Exception:
            pass
        self.sent = dict(wire.sent)

    async def _await_suback(self, reader: asyncio.Task, subscribed: asyncio.Future) -> None:
        done, _ = await asyncio.wait({reader, subscribed}, timeout=self.timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if subscribed in done:
            subscribed.result()                # raises when the broker refused the subscription
            return
        if reader in done:
            exc = reader.exception()
            raise exc if exc is not None else MqttError("source.mqtt.closed", "connection closed")
        raise MqttError("source.mqtt.timeout", "no SUBACK")

    async def _read_loop(self, wire: _Wire, subscribed: asyncio.Future) -> None:
        while True:
            ptype, flags, n, hlen = await wire.read_header()
            if ptype == PUBLISH:
                want = 32 if self.sys_topics else 0
                topic, payload = await wire.read_publish(flags, n, want)
                if topic.startswith("$SYS/"):
                    field = SYS_STATS.get(topic)
                    if field:
                        v = _sys_value(payload)
                        if v is not None:
                            self.broker[field] = v
                self.counter.add(topic, hlen + n, retained=bool(flags & 1))
            elif ptype == SUBACK:
                body = await wire.read_small(n)
                codes = body[2:]
                if not subscribed.done():
                    if not codes or codes[0] == 0x80:
                        subscribed.set_exception(MqttError("source.mqtt.subscribe_denied", "subscription refused"))
                    else:
                        subscribed.set_result(True)
            elif ptype == PINGRESP:
                await wire.read_small(n)
            else:
                await wire._skip(n)            # anything else from the broker is ignored

    async def _session(self, emit, status: StatusTracker) -> None:
        wire = await self._connect()
        subscribed = asyncio.get_running_loop().create_future()
        reader = asyncio.create_task(self._read_loop(wire, subscribed))
        try:
            await self._await_suback(reader, subscribed)
            await status.set(emit, True, "source.mqtt.ok")
            last_ping = time.monotonic()
            next_tick = time.monotonic() + self.interval
            while True:
                done, _ = await asyncio.wait({reader}, timeout=max(0.0, next_tick - time.monotonic()))
                if done:
                    exc = reader.exception()
                    raise exc if exc is not None else MqttError("source.mqtt.closed", "connection closed")
                mono = time.monotonic()
                if mono >= next_tick:
                    frame = self.counter.take(mono)
                    if self.sys_topics and self.broker:
                        frame["broker"] = dict(self.broker)
                    await emit_event(emit, frame)
                    next_tick += self.interval
                    if next_tick < mono:
                        next_tick = mono + self.interval
                if mono - last_ping >= KEEPALIVE_S * 0.8:
                    await wire.write(pingreq_packet())
                    last_ping = mono
                if mono - wire.last_rx > KEEPALIVE_S * 2 + 5:
                    raise MqttError("source.mqtt.timeout", "broker silent")
        finally:
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass
            if not subscribed.done():
                subscribed.cancel()
            elif not subscribed.cancelled():
                subscribed.exception()          # mark as retrieved
            await self._close(wire)

    async def run(self, emit) -> None:
        await run_with_backoff(SOURCE, MODULE, emit, self._session)

    async def test(self):
        async def probe():
            wire = await self._connect()
            subscribed = asyncio.get_running_loop().create_future()
            counter, self.counter = self.counter, Counter(self.counter.levels)
            reader = asyncio.create_task(self._read_loop(wire, subscribed))
            try:
                await self._await_suback(reader, subscribed)
                await asyncio.sleep(1.0)       # collect retained topics for a moment
                c = self.counter
                details = {"topics": len(c.open), "messages": c.total, "retained": c.retained}
            finally:
                reader.cancel()
                try:
                    await reader
                except (asyncio.CancelledError, Exception):
                    pass
                if not subscribed.done():
                    subscribed.cancel()
                elif not subscribed.cancelled():
                    subscribed.exception()      # mark as retrieved
                await self._close(wire)
                self.counter = counter
            return details
        return await short_test(MODULE, probe(), self.timeout * 2 + 5)
