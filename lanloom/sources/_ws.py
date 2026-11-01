"""Minimal asyncio WebSocket client (RFC 6455), standard library only.

Handshake with Sec-WebSocket-Accept check, masked client frames, fragmented messages, ping/pong, close,
read size cap. Text messages only towards the caller; binary messages are refused. This module knows nothing
about what is sent: the hub source enforces its own allow-list at its single send function.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct
import time
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 32 * 1024 * 1024
MAX_HANDSHAKE = 16 * 1024


class WsError(Exception):
    """Protocol or connection failure (texts never contain request headers)."""


class WsClosed(WsError):
    pass


def make_ssl_context(allow_self_signed: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if allow_self_signed:              # explicit opt-in only (e.g. a hub with its own certificate)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _mask(data: bytes, key: bytes) -> bytes:
    n = len(data)
    if not n:
        return b""
    m = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")


def accept_for(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, max_size: int):
        self._reader = reader
        self._writer = writer
        self.max_size = max_size
        self._wlock = asyncio.Lock()
        self.last_rx = time.monotonic()
        self.closed = False

    @classmethod
    async def connect(cls, url: str, *, timeout: float = 10.0, ssl_context: ssl.SSLContext | None = None,
                      max_size: int = MAX_MESSAGE, user_agent: str = "lanloom") -> "WebSocket":
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss") or not u.hostname:
            raise WsError("URL must be ws:// or wss://")
        secure = u.scheme == "wss"
        port = u.port or (443 if secure else 80)
        ctx = (ssl_context or make_ssl_context()) if secure else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(u.hostname, port, ssl=ctx, server_hostname=u.hostname if secure else None,
                                    limit=MAX_HANDSHAKE * 4), timeout)
        ws = cls(reader, writer, max_size)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            host = "[%s]" % u.hostname if ":" in u.hostname else u.hostname
            if u.port:
                host += ":%d" % u.port
            path = (u.path or "/") + ("?" + u.query if u.query else "")
            lines = ["GET %s HTTP/1.1" % path, "Host: " + host, "Upgrade: websocket", "Connection: Upgrade",
                     "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13", "User-Agent: " + user_agent]
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
            await writer.drain()
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
            except asyncio.LimitOverrunError:
                raise WsError("handshake reply too large") from None
            except asyncio.IncompleteReadError:
                raise WsClosed("connection closed during handshake") from None
            if len(head) > MAX_HANDSHAKE:
                raise WsError("handshake reply too large")
            text = head.decode("latin-1")
            first, _, rest = text.partition("\r\n")
            parts = first.split(" ")
            if len(parts) < 2 or parts[1] != "101":
                raise WsError("handshake refused (HTTP %s)" % (parts[1][:3] if len(parts) > 1 else "?"))
            fields = {}
            for line in rest.split("\r\n"):
                k, _, v = line.partition(":")
                fields[k.strip().lower()] = v.strip()
            if fields.get("sec-websocket-accept") != accept_for(key):
                raise WsError("handshake: Sec-WebSocket-Accept mismatch")
            if "upgrade" not in fields.get("connection", "").lower() or fields.get("upgrade", "").lower() != "websocket":
                raise WsError("handshake: no WebSocket upgrade")
        except BaseException:
            await ws._abort()
            raise
        ws.last_rx = time.monotonic()
        return ws

    # ------------------------------------------------------------ write
    async def _frame(self, opcode: int, payload: bytes) -> None:
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack("!H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack("!Q", n)
        key = os.urandom(4)
        async with self._wlock:
            if self.closed:
                raise WsClosed("not connected")
            self._writer.write(bytes(head) + key + _mask(payload, key))
            await self._writer.drain()

    async def send_text(self, text: str) -> None:
        await self._frame(0x1, text.encode("utf-8"))

    async def ping(self) -> None:
        await self._frame(0x9, b"ll")

    # ------------------------------------------------------------ read
    async def _read_frame(self):
        try:
            b1, b2 = await self._reader.readexactly(2)
            fin, opcode = bool(b1 & 0x80), b1 & 0x0F
            if b1 & 0x70:
                raise WsError("extension bits without negotiation")
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", await self._reader.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", await self._reader.readexactly(8))[0]
            if n > self.max_size:
                raise WsError("frame too large")
            key = await self._reader.readexactly(4) if b2 & 0x80 else None
            data = await self._reader.readexactly(n) if n else b""
        except asyncio.IncompleteReadError:
            self.closed = True
            raise WsClosed("connection closed") from None
        if key:
            data = _mask(data, key)
        return fin, opcode, data

    async def recv(self) -> str:
        """Next text message. Answers pings, joins fragments, raises WsClosed on close."""
        parts, size = [], 0
        while True:
            fin, opcode, data = await self._read_frame()
            self.last_rx = time.monotonic()
            if opcode == 0x9:
                await self._frame(0xA, data[:125])
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                try:
                    await self._frame(0x8, data[:2])
                except (OSError, WsError):
                    pass
                await self._abort()
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005
                raise WsClosed("peer closed (%d)" % code)
            if opcode == 0x1:
                if parts:
                    raise WsError("continuation frame expected")
                parts = [data]
            elif opcode == 0x0:
                if not parts:
                    raise WsError("continuation without start")
                parts.append(data)
            elif opcode == 0x2:
                raise WsError("binary messages not supported")
            else:
                raise WsError("unknown opcode %d" % opcode)
            size += len(data)
            if size > self.max_size:
                raise WsError("message too large")
            if fin:
                try:
                    return b"".join(parts).decode("utf-8")
                except UnicodeDecodeError:
                    raise WsError("invalid UTF-8 in text message") from None

    # ------------------------------------------------------------ close
    async def _abort(self) -> None:
        self.closed = True
        try:
            self._writer.close()
        except Exception:
            pass

    async def close(self) -> None:
        if not self.closed:
            try:
                await asyncio.wait_for(self._frame(0x8, struct.pack("!H", 1000)), 2)
            except (OSError, WsError, asyncio.TimeoutError):
                pass
        await self._abort()
