"""Read-only live source for a home-automation hub with a WebSocket API.

The hub has no read-only tokens, so this module guarantees read-only access itself: there is exactly one send
path (``Hub._send``) and it only lets through the message types in ``ALLOWED_TYPES``, each with a fixed set of
fields; ``subscribe_events`` additionally only for the event types in ``ALLOWED_EVENTS``. Nothing else can be
sent - a static test checks this module's source.

Emits:
* ``hub_snapshot`` every 10 minutes (and right after connecting): devices, entities, areas;
* ``hub_state`` ``{"entity_id", "state", "t"}`` when an entity's state changes, coalesced to at most 20 per second
  (the latest value per entity wins).

The token is kept in a private attribute only; it never appears in logs, exception texts, events or ``repr()``.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.parse

from ._common import SourceError, StatusTracker, clip, emit_event, now, run_with_backoff, short_test
from ._ws import WebSocket, WsClosed, WsError, make_ssl_context

MODULE = "hub"
SOURCE = "hub"
WS_PATH = "/api/websocket"  # identifier: hub WebSocket path

# Exactly these message types may be sent to the hub, each with exactly these fields.
ALLOWED_TYPES = frozenset({
    "auth",
    "get_states",
    "get_config",
    "config/device_registry/list",
    "config/entity_registry/list",
    "config/area_registry/list",
    "subscribe_events",
})
ALLOWED_FIELDS = {
    "auth": frozenset({"access_token"}),
    "subscribe_events": frozenset({"event_type"}),
}
ALLOWED_EVENTS = frozenset({"state_changed"})

SNAPSHOT_INTERVAL_S = 600.0
STATE_RATE = 20                  # hub_state events per second at most
HEARTBEAT_S = 30.0               # WebSocket ping frame (transport level, not a hub message)
SILENCE_S = 95.0
REQUEST_TIMEOUT_S = 30.0
MAX_DEVICES = 5000
MAX_ENTITIES = 20000
MAX_AREAS = 500
MAX_PENDING = 20000


class ForbiddenMessage(Exception):
    """A message outside the allow-list was about to be sent - refused, nothing was sent."""


def hub_ws_url(url: str) -> str:
    """User-facing hub address (http/https/ws/wss, with or without path) -> WebSocket URL."""
    text = str(url or "").strip()
    if not text:
        raise SourceError("source.hub.config", "hub URL missing")
    if "://" not in text:
        text = "http://" + text
    u = urllib.parse.urlsplit(text)
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(u.scheme.lower())
    if scheme is None or not u.hostname:
        raise SourceError("source.hub.config", "hub URL must be http(s):// or ws(s)://")
    if u.username or u.password:
        raise SourceError("source.hub.config", "credentials in the URL are not allowed")
    path = u.path if u.path not in ("", "/") else WS_PATH
    netloc = u.hostname if ":" not in u.hostname else "[%s]" % u.hostname
    if u.port:
        netloc += ":%d" % u.port
    return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))


class _Conn:
    """One authenticated connection: request/response matching and an event callback."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.on_event = None
        self.reader: asyncio.Task | None = None

    def start(self, on_event) -> None:
        self.on_event = on_event
        self.reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while True:
                text = await self.ws.recv()
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                for m in data if isinstance(data, list) else [data]:
                    if not isinstance(m, dict):
                        continue
                    kind = m.get("type")
                    if kind == "result":
                        fut = self.pending.pop(m.get("id"), None)
                        if fut is not None and not fut.done():
                            fut.set_result(m)
                    elif kind == "event" and self.on_event is not None:
                        self.on_event(m)
        finally:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(SourceError("source.hub.closed", "connection closed"))
            self.pending.clear()

    async def close(self) -> None:
        if self.reader is not None:
            self.reader.cancel()
            try:
                await self.reader
            except (asyncio.CancelledError, Exception):
                pass
        await self.ws.close()


class Hub:
    """Live source: home-automation hub (WebSocket API), strictly read-only."""

    name = SOURCE

    def __init__(self, url, token, allow_self_signed=False, snapshot_interval=SNAPSHOT_INTERVAL_S,
                 state_rate=STATE_RATE, timeout=10.0):
        self._ws_url = hub_ws_url(url)
        tok = token if isinstance(token, str) else ""
        tok = tok.strip()
        if not tok or len(tok) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in tok):
            raise SourceError("source.hub.config", "token missing or invalid")
        self._token = tok
        self._allow_self_signed = bool(allow_self_signed)
        self.snapshot_interval = max(30.0, float(snapshot_interval))
        self.state_rate = max(1, int(state_rate))
        self.timeout = max(1.0, float(timeout))
        self.version = None
        self.sent: dict[str, int] = {}       # counters per sent type (diagnostics, no content)
        self.refused = 0
        self._states: dict[str, list] = {}   # entity_id -> [state, unit, friendly_name]
        self._pending: dict[str, tuple] = {}  # entity_id -> (state, t) waiting for the rate limit
        self._tokens = float(self.state_rate)
        self._tokens_t = time.monotonic()

    def __repr__(self):
        return "Hub(url=%r)" % self._ws_url

    __str__ = __repr__

    # ------------------------------------------------------------ the single send path to the hub
    async def _send(self, conn: _Conn, type_: str, **fields):
        """Send one allowed message. Returns a future for the result (None for ``auth``)."""
        if type_ not in ALLOWED_TYPES:
            self.refused += 1
            raise ForbiddenMessage(str(type_)[:60])
        if set(fields) - ALLOWED_FIELDS.get(type_, frozenset()):
            self.refused += 1
            raise ForbiddenMessage(str(type_)[:60])
        if type_ == "subscribe_events" and fields.get("event_type") not in ALLOWED_EVENTS:
            self.refused += 1
            raise ForbiddenMessage(type_)
        message = dict(fields)
        message["type"] = type_
        fut = None
        if type_ != "auth":
            conn.next_id += 1
            message["id"] = conn.next_id
            fut = asyncio.get_running_loop().create_future()
            conn.pending[conn.next_id] = fut
        await conn.ws.send_text(json.dumps(message))
        self.sent[type_] = self.sent.get(type_, 0) + 1
        return fut

    async def _request(self, conn: _Conn, type_: str, **fields):
        """Send and await the result payload. Raises SourceError when the hub refuses."""
        fut = await self._send(conn, type_, **fields)
        try:
            m = await asyncio.wait_for(fut, REQUEST_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise SourceError("source.hub.timeout", "no reply") from None
        if not m.get("success"):
            code = (m.get("error") or {}).get("code") if isinstance(m.get("error"), dict) else None
            key = "source.hub.forbidden" if code == "unauthorized" else "source.hub.request_failed"
            raise SourceError(key, "request refused")
        return m.get("result")

    # ------------------------------------------------------------ connect + auth
    async def _recv_json(self, ws: WebSocket) -> dict:
        try:
            text = await asyncio.wait_for(ws.recv(), self.timeout)
        except WsClosed:
            raise SourceError("source.hub.closed", "connection closed") from None
        try:
            m = json.loads(text)
        except ValueError:
            raise SourceError("source.hub.protocol", "invalid JSON") from None
        if not isinstance(m, dict):
            raise SourceError("source.hub.protocol", "unexpected message")
        return m

    async def _open(self) -> _Conn:
        ctx = make_ssl_context(self._allow_self_signed) if self._ws_url.startswith("wss:") else None
        try:
            ws = await WebSocket.connect(self._ws_url, timeout=self.timeout, ssl_context=ctx)
        except WsError:
            raise SourceError("source.hub.protocol", "WebSocket handshake failed") from None
        conn = _Conn(ws)
        try:
            m = await self._recv_json(ws)
            if m.get("type") != "auth_required":
                raise SourceError("source.hub.protocol", "hub did not ask for authentication")
            await self._send(conn, "auth", access_token=self._token)
            m = await self._recv_json(ws)
            if m.get("type") == "auth_invalid":
                raise SourceError("source.hub.auth_failed", "authentication refused")
            if m.get("type") != "auth_ok":
                raise SourceError("source.hub.protocol", "unexpected authentication reply")
            self.version = clip(m.get("ha_version"), 32)  # identifier: hub auth reply field
        except BaseException:
            await ws.close()
            raise
        return conn

    # ------------------------------------------------------------ data
    def _on_event(self, m: dict) -> None:
        ev = m.get("event")
        if not isinstance(ev, dict) or ev.get("event_type") != "state_changed":
            return
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        eid = data.get("entity_id")
        if not isinstance(eid, str) or not eid or len(eid) > 255:
            return
        new = data.get("new_state") if isinstance(data.get("new_state"), dict) else None
        old = data.get("old_state") if isinstance(data.get("old_state"), dict) else None
        if new is None:
            self._states.pop(eid, None)
            state = None
        else:
            state = clip(new.get("state"), 255)
            attrs = new.get("attributes") if isinstance(new.get("attributes"), dict) else {}
            if eid in self._states or len(self._states) < MAX_ENTITIES:
                self._states[eid] = [state, clip(attrs.get("unit_of_measurement"), 32),
                                     clip(attrs.get("friendly_name"))]
        if old is not None and new is not None and old.get("state") == new.get("state"):
            return                           # attribute-only change: not a state change
        if eid in self._pending or len(self._pending) < MAX_PENDING:
            self._pending[eid] = (state, now())

    def take_states(self, mono: float | None = None) -> list[dict]:
        """Rate-limited hub_state events (token bucket, ``state_rate`` per second, latest value per entity)."""
        mono = time.monotonic() if mono is None else mono
        self._tokens = min(float(self.state_rate), self._tokens + (mono - self._tokens_t) * self.state_rate)
        self._tokens_t = mono
        out = []
        while self._pending and self._tokens >= 1.0:
            eid = next(iter(self._pending))
            state, t = self._pending.pop(eid)
            self._tokens -= 1.0
            out.append({"type": "hub_state", "source": SOURCE, "entity_id": eid, "state": state, "t": t})
        return out

    def _store_states(self, result) -> None:
        states = {}
        for s in result if isinstance(result, list) else []:
            if not isinstance(s, dict) or not isinstance(s.get("entity_id"), str):
                continue
            if len(states) >= MAX_ENTITIES:
                break
            attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
            states[s["entity_id"][:255]] = [clip(s.get("state")), clip(attrs.get("unit_of_measurement"), 32),
                                            clip(attrs.get("friendly_name"))]
        self._states = states

    async def _registry(self, conn: _Conn, type_: str):
        try:
            result = await self._request(conn, type_)
        except SourceError as exc:
            if exc.key == "source.hub.forbidden":
                return None              # token without admin rights: registry not readable
            raise
        return result if isinstance(result, list) else []

    async def snapshot(self, conn: _Conn) -> dict:
        self._store_states(await self._request(conn, "get_states"))
        devices_raw = await self._registry(conn, "config/device_registry/list")
        entities_raw = await self._registry(conn, "config/entity_registry/list")
        areas_raw = await self._registry(conn, "config/area_registry/list")
        return build_snapshot(self._states, devices_raw, entities_raw, areas_raw)

    # ------------------------------------------------------------ run / test
    async def _session(self, emit, status: StatusTracker) -> None:
        conn = await self._open()
        try:
            conn.start(self._on_event)
            await self._request(conn, "subscribe_events", event_type="state_changed")
            await emit_event(emit, await self.snapshot(conn))
            await status.set(emit, True, "source.hub.ok")
            next_snapshot = time.monotonic() + self.snapshot_interval
            last_ping = time.monotonic()
            while True:
                done, _ = await asyncio.wait({conn.reader}, timeout=0.1)
                if done:
                    exc = conn.reader.exception()
                    if isinstance(exc, (WsError, SourceError)) or exc is None:
                        raise SourceError("source.hub.closed", "connection closed")
                    raise exc
                for ev in self.take_states():
                    await emit_event(emit, ev)
                mono = time.monotonic()
                if mono >= next_snapshot:
                    next_snapshot = mono + self.snapshot_interval
                    await emit_event(emit, await self.snapshot(conn))
                if mono - last_ping >= HEARTBEAT_S:
                    await conn.ws.ping()
                    last_ping = mono
                if mono - conn.ws.last_rx > SILENCE_S:
                    raise SourceError("source.hub.timeout", "hub silent")
        finally:
            await conn.close()

    async def run(self, emit) -> None:
        await run_with_backoff(SOURCE, MODULE, emit, self._session)

    async def test(self):
        async def probe():
            conn = await self._open()
            try:
                conn.start(None)
                config = await self._request(conn, "get_config")
                states = await self._request(conn, "get_states")
                devices = await self._registry(conn, "config/device_registry/list")
            finally:
                await conn.close()
            version = clip(config.get("version"), 32) if isinstance(config, dict) else None
            return {"version": version or self.version,
                    "entities": len(states) if isinstance(states, list) else 0,
                    "devices": len(devices) if isinstance(devices, list) else None}
        return await short_test(MODULE, probe(), self.timeout * 3 + REQUEST_TIMEOUT_S)


def build_snapshot(states: dict, devices_raw, entities_raw, areas_raw) -> dict:
    """Registry lists + states -> hub_snapshot event (only the fields the map needs)."""
    areas = []
    for a in (areas_raw or [])[:MAX_AREAS]:
        if isinstance(a, dict) and a.get("area_id"):
            areas.append({"id": clip(a.get("area_id"), 64), "name": clip(a.get("name"))})
    devices, device_area = [], {}
    for d in devices_raw or []:
        if len(devices) >= MAX_DEVICES:
            break
        if not isinstance(d, dict) or not d.get("id") or d.get("disabled_by"):
            continue
        did = clip(d.get("id"), 64)
        device_area[did] = clip(d.get("area_id"), 64)
        devices.append({"id": did, "name": clip(d.get("name_by_user") or d.get("name")),
                        "model": clip(d.get("model")), "manufacturer": clip(d.get("manufacturer")),
                        "area": device_area[did], "via": clip(d.get("via_device_id"), 64),
                        "service": d.get("entry_type") == "service"})
    entities, seen = [], set()
    for e in entities_raw or []:
        if len(entities) >= MAX_ENTITIES:
            break
        if not isinstance(e, dict) or not isinstance(e.get("entity_id"), str) or e.get("disabled_by"):
            continue
        eid = e["entity_id"][:255]
        seen.add(eid)
        st = states.get(eid) or [None, None, None]
        did = clip(e.get("device_id"), 64)
        entities.append({"entity_id": eid, "device_id": did,
                         "name": clip(e.get("name") or e.get("original_name") or st[2]),
                         "state": st[0], "unit": st[1], "domain": eid.split(".", 1)[0],
                         "area": clip(e.get("area_id"), 64) or device_area.get(did)})
    for eid, st in states.items():          # entities without registry entry (or registry not readable)
        if len(entities) >= MAX_ENTITIES:
            break
        if eid in seen:
            continue
        entities.append({"entity_id": eid, "device_id": None, "name": st[2], "state": st[0], "unit": st[1],
                         "domain": eid.split(".", 1)[0], "area": None})
    snap = {"type": "hub_snapshot", "source": SOURCE, "t": now(), "devices": devices, "entities": entities,
            "areas": areas}
    if devices_raw is None or entities_raw is None:
        snap["limited"] = True               # registries not readable with this token
    return snap
