"""Read-only live source for a home-automation hub with a WebSocket API (the hub bridge).

The hub has no read-only tokens, so this module guarantees read-only access itself: there is exactly one send
path (``Hub._send``) and it only lets through the message types in ``ALLOWED_TYPES``, each with a fixed set of
fields (``ALLOWED_FIELDS``) whose values are checked (``_value_ok``); ``subscribe_events`` additionally only for the
event types in ``ALLOWED_EVENTS``. The types in ``ADMIN_TYPES`` (automation and script configuration, traces,
related items) go out only when the user switched on *admin mode* for this connection. Nothing else can be sent -
static tests check this module's source. Templates are never rendered (the hub would have to evaluate every one
of them on every call; the allow-list has no such type).

Load protection: full inventory every 10 minutes; configuration reads, related-item lookups and traces each have
their own budget per minute (``CONFIG_PER_MIN``, ``RELATED_PER_MIN``, ``TRACE_PER_MIN``); traces only on request,
per automation; answers that are still fresh are not asked again; scripts that call each other in a loop are never
looked up (the hub resolves them recursively); an item the hub answers with an error is left alone with growing
pauses (``hub_deps.Blocklist``); a message type the hub does not know is not sent again in this session.

Privacy (``lanloom/hub_privacy.py``): people, trackers, zones, cameras, pictures, calendars, to-do lists and
notifications are dropped before anything is stored, unless the user switched a group on. A tracker only lends its
hardware/IP address for matching devices. Events carry the entity id, the state and the time, plus ``by`` when an
automation or script caused the change (its entity id) or a person did (only the word ``user``); service data,
user ids and context ids never leave this module. Attributes are dropped except unit and name.

Emits:
* ``hub_snapshot`` every 10 minutes (and right after connecting): devices (with hardware addresses from the device
  registry and the integration domains), entities, areas, tracker links, automations and scripts (mode, last run);
* ``hub_state`` ``{"entity_id", "state", "t", "by"?}`` when an entity's state changes, coalesced to at most 20 per
  second (the latest value per entity wins);
* ``hub_triggers`` every 10 seconds: how often each automation/script ran since the last frame;
* ``hub_deps`` (admin mode): entity ids each automation/script names per role, related items, script loops, blocks;
* ``hub_traces`` (admin mode, on request): the last runs of one automation/script and the step path of the newest;
* ``hub_bridge`` every minute: counters of the bridge (sent per type, refused, private entities dropped, budgets).

The token is kept in a private attribute only; it never appears in logs, exception texts, events or ``repr()``.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from collections import OrderedDict

from .. import hub_privacy as privacy
from . import hub_deps as deps
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
    "config_entries/get",
    "subscribe_events",
    "automation/config",
    "script/config",
    "trace/list",
    "trace/get",
    "search/related",
})
ALLOWED_FIELDS = {
    "auth": frozenset({"access_token"}),
    "subscribe_events": frozenset({"event_type"}),
    "automation/config": frozenset({"entity_id"}),
    "script/config": frozenset({"entity_id"}),
    "trace/list": frozenset({"domain", "item_id"}),
    "trace/get": frozenset({"domain", "item_id", "run_id"}),
    "search/related": frozenset({"item_type", "item_id"}),
}
ALLOWED_EVENTS = frozenset({"state_changed", "automation_triggered", "script_started"})
# Only with admin mode switched on for the connection (they need an administrator token on the hub anyway).
ADMIN_TYPES = frozenset({"automation/config", "script/config", "trace/list", "trace/get", "search/related"})
ITEM_DOMAINS = ("automation", "script")
ITEM_ID_RX = re.compile(r"\A[^\x00-\x1f\x7f]{1,255}\Z")
RUN_ID_RX = re.compile(r"\A[0-9A-Za-z_-]{1,64}\Z")
STEP_RX = re.compile(r"\A[a-z_]{1,24}(?:/[0-9a-z_]{1,24}){0,12}\Z")

SNAPSHOT_INTERVAL_S = 600.0
STATE_RATE = 20                  # hub_state events per second at most
TRIGGER_FRAME_S = 10.0
BRIDGE_FRAME_S = 60.0
HEARTBEAT_S = 30.0               # WebSocket ping frame (transport level, not a hub message)
SILENCE_S = 95.0
REQUEST_TIMEOUT_S = 30.0
MAX_DEVICES = 5000
MAX_ENTITIES = 20000
MAX_AREAS = 500
MAX_PENDING = 20000
MAX_ITEMS = 3000                 # automations + scripts followed in admin mode
MAX_CONTEXTS = 5000              # recent automation/script runs, to say "by" for the changes they cause
CONTEXT_TTL_S = 600.0
MAX_TRACE_WANTED = 20
MAX_TRACE_RUNS = 20
MAX_TRACE_STEPS = 100
# load protection of the admin-mode lookups
CONFIG_PER_MIN = 30
RELATED_PER_MIN = 20
TRACE_PER_MIN = 6
CONFIG_FRESH_S = 3600.0
RELATED_FRESH_S = 7200.0
DEPS_FRAME_S = 60.0


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


def _value_ok(type_: str, key: str, value) -> bool:
    """Values of the allowed fields: event types from the list, items only of automations and scripts."""
    if key == "access_token":
        return isinstance(value, str)
    if key == "event_type":
        return value in ALLOWED_EVENTS
    if type_ in ("automation/config", "script/config"):
        want = type_.split("/", 1)[0]
        return privacy.valid_entity_id(value) and privacy.domain_of(value) == want
    if key in ("domain", "item_type"):
        return value in ITEM_DOMAINS
    if key == "item_id":
        if type_ == "search/related":
            return privacy.valid_entity_id(value) and privacy.domain_of(value) in ITEM_DOMAINS
        return isinstance(value, str) and bool(ITEM_ID_RX.match(value))
    if key == "run_id":
        return isinstance(value, str) and bool(RUN_ID_RX.match(value))
    return False


def _ts(value):
    """Hub time stamp (ISO 8601) -> Unix seconds or None."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        import datetime
        return round(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp(), 3)
    except ValueError:
        return None


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
                 state_rate=STATE_RATE, timeout=10.0, admin_mode=False, show=frozenset(), clock=time.monotonic):
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
        self.admin_mode = bool(admin_mode)
        self.show = frozenset(d for d in (show or ()) if d in privacy.PRIVATE_DOMAINS)
        self._clock = clock
        self.version = None
        self.sent: dict[str, int] = {}       # counters per sent type (diagnostics, no content)
        self.refused = 0
        self.private_dropped = 0             # private entities and events dropped by the privacy filter
        self._states: dict[str, list] = {}   # entity_id -> [state, unit, friendly_name]
        self._pending: dict[str, tuple] = {}  # entity_id -> (state, t, by) waiting for the rate limit
        self._tokens = float(self.state_rate)
        self._tokens_t = clock()
        self._trackers: dict[str, dict] = {}  # device_tracker entity -> {"mac", "ip"} (never state or place)
        self._items: dict[str, dict] = {}     # automation/script entity -> {"id", "mode", "last", "state"}
        self._ctx: OrderedDict = OrderedDict()   # run context id -> (automation/script entity, monotonic time)
        self._runs: dict[str, list] = {}      # automation/script entity -> [count since last frame, last time]
        # admin mode
        self._refs: dict[str, dict] = {}      # item -> {"trigger", "condition", "action", "text", "fp", "t"}
        self._related: dict[str, dict] = {}   # item -> {"ids", "t", "fp"}
        self._due_config: dict[str, float] = {}
        self._due_related: dict[str, float] = {}
        self._cycles: list = []
        self._blocks = deps.Blocklist()
        self._dead_types: set = set()         # message types the hub answered with "unknown command"
        self._budgets = {"config": deps.Budget(CONFIG_PER_MIN, mono=clock()),
                         "related": deps.Budget(RELATED_PER_MIN, mono=clock()),
                         "trace": deps.Budget(TRACE_PER_MIN, burst=2, mono=clock())}
        self._trace_wanted: OrderedDict = OrderedDict()
        self._deps_dirty = False

    def __repr__(self):
        return "Hub(url=%r)" % self._ws_url

    __str__ = __repr__

    # ------------------------------------------------------------ the single send path to the hub
    async def _send(self, conn: _Conn, type_: str, **fields):
        """Send one allowed message. Returns a future for the result (None for ``auth``)."""
        if type_ not in ALLOWED_TYPES:
            self.refused += 1
            raise ForbiddenMessage(str(type_)[:60])
        if set(fields) != ALLOWED_FIELDS.get(type_, frozenset()):      # exactly the fields of the type, all of them
            self.refused += 1
            raise ForbiddenMessage(str(type_)[:60])
        if any(not _value_ok(type_, k, v) for k, v in fields.items()):
            self.refused += 1
            raise ForbiddenMessage(type_)
        if type_ == "subscribe_events" and fields.get("event_type") not in ALLOWED_EVENTS:
            self.refused += 1
            raise ForbiddenMessage(type_)
        if type_ in ADMIN_TYPES and not self.admin_mode:
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
            conn.pending = {k: f for k, f in conn.pending.items() if f is not fut}
            raise SourceError("source.hub.timeout", "no reply") from None
        if not m.get("success"):
            code = (m.get("error") or {}).get("code") if isinstance(m.get("error"), dict) else None
            if code == "unknown_command":
                self._dead_types.add(type_)
                raise SourceError("source.hub.unknown_command", "unknown command")
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

    # ------------------------------------------------------------ events
    def _by(self, context) -> str | None:
        """Who caused a change: an automation/script run we saw (its entity id), a person (``user``), else None."""
        if not isinstance(context, dict):
            return None
        mono = self._clock()
        for key in ("id", "parent_id"):
            cid = context.get(key)
            hit = self._ctx.get(cid) if isinstance(cid, str) else None
            if hit and mono - hit[1] <= CONTEXT_TTL_S:
                return hit[0]
        return "user" if context.get("user_id") else None

    def _remember_run(self, eid: str, context) -> None:
        cid = context.get("id") if isinstance(context, dict) else None
        if isinstance(cid, str) and 0 < len(cid) <= 64:
            self._ctx[cid] = (eid, self._clock())
            self._ctx.move_to_end(cid)
            while len(self._ctx) > MAX_CONTEXTS:
                self._ctx.popitem(last=False)
        run = self._runs.get(eid)
        if run is None and len(self._runs) >= MAX_ITEMS:
            return
        run = self._runs.setdefault(eid, [0, None])
        run[0] += 1
        run[1] = now()

    def _on_event(self, m: dict) -> None:
        ev = m.get("event")
        if not isinstance(ev, dict):
            return
        etype = ev.get("event_type")
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if etype in ("automation_triggered", "script_started"):
            eid = data.get("entity_id")
            want = "automation" if etype == "automation_triggered" else "script"
            if privacy.valid_entity_id(eid) and privacy.domain_of(eid) == want:
                self._remember_run(eid, ev.get("context"))
            return                           # name, source and variables of the run are never kept
        if etype != "state_changed":
            return
        eid = data.get("entity_id")
        if not isinstance(eid, str) or not eid or len(eid) > 255:
            return
        new = data.get("new_state") if isinstance(data.get("new_state"), dict) else None
        old = data.get("old_state") if isinstance(data.get("old_state"), dict) else None
        attrs = new.get("attributes") if new and isinstance(new.get("attributes"), dict) else {}
        if privacy.domain_of(eid) == "device_tracker":
            link = privacy.tracker_link(eid, attrs)
            if link and (eid in self._trackers or len(self._trackers) < MAX_ENTITIES):
                self._trackers[eid] = {"mac": link["mac"], "ip": link["ip"]}
        if privacy.is_private(eid, self.show):
            self.private_dropped += 1
            self._states.pop(eid, None)
            return
        if new is None:
            self._states.pop(eid, None)
            self._items.pop(eid, None)
            state = None
        else:
            state = clip(new.get("state"), 255)
            if eid in self._states or len(self._states) < MAX_ENTITIES:
                self._states[eid] = [state, clip(attrs.get("unit_of_measurement"), 32), clip(attrs.get("friendly_name"))]
            if privacy.domain_of(eid) in ITEM_DOMAINS:
                self._note_item(eid, state, attrs)
        if old is not None and new is not None and old.get("state") == new.get("state"):
            return                           # attribute-only change: not a state change
        if eid in self._pending or len(self._pending) < MAX_PENDING:
            self._pending[eid] = (state, now(), self._by((new or {}).get("context")))

    def _note_item(self, eid: str, state, attrs: dict) -> None:
        if eid not in self._items and len(self._items) >= MAX_ITEMS:
            return
        dom = privacy.domain_of(eid)
        item_id = attrs.get("id") if dom == "automation" else eid.split(".", 1)[1]
        mode = attrs.get("mode")
        self._items[eid] = {"id": str(item_id)[:255] if isinstance(item_id, (str, int)) and str(item_id) else None,
                            "mode": mode if mode in ("single", "restart", "queued", "parallel") else None,
                            "last": _ts(attrs.get("last_triggered")), "state": state}

    def take_states(self, mono: float | None = None) -> list[dict]:
        """Rate-limited hub_state events (token bucket, ``state_rate`` per second, latest value per entity)."""
        mono = self._clock() if mono is None else mono
        self._tokens = min(float(self.state_rate), self._tokens + max(0.0, mono - self._tokens_t) * self.state_rate)
        self._tokens_t = mono
        out = []
        while self._pending and self._tokens >= 1.0:
            eid = next(iter(self._pending))
            state, t, by = self._pending.pop(eid)
            self._tokens -= 1.0
            ev = {"type": "hub_state", "source": SOURCE, "entity_id": eid, "state": state, "t": t}
            if by:
                ev["by"] = by
            out.append(ev)
        return out

    def take_triggers(self) -> dict | None:
        runs = {e: r for e, r in self._runs.items() if r[0]}
        if not runs:
            return None
        frame = {"type": "hub_triggers", "source": SOURCE, "t": now(),
                 "runs": {e: {"n": r[0], "last": r[1]} for e, r in runs.items()}}
        for r in runs.values():
            r[0] = 0
        return frame

    def bridge_frame(self) -> dict:
        return {"type": "hub_bridge", "source": SOURCE, "t": now(), "admin": self.admin_mode,
                "sent": dict(self.sent), "refused": self.refused, "private_dropped": self.private_dropped,
                "privacy": privacy.summary(self.show), "unknown_types": sorted(self._dead_types),
                "budgets": {k: {"per_min": round(b.rate * 60), "waits": b.waits} for k, b in self._budgets.items()},
                "blocked": len(self._blocks.active(time.time())), "trace_queue": len(self._trace_wanted),
                "items": len(self._items), "trackers": len(self._trackers)}

    # ------------------------------------------------------------ snapshot
    def _store_states(self, result) -> None:
        states, trackers, items = {}, {}, {}
        dropped = 0
        for s in result if isinstance(result, list) else []:
            if not isinstance(s, dict) or not isinstance(s.get("entity_id"), str):
                continue
            eid = s["entity_id"][:255]
            attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
            if privacy.domain_of(eid) == "device_tracker" and len(trackers) < MAX_ENTITIES:
                link = privacy.tracker_link(eid, attrs)
                if link:
                    trackers[eid] = {"mac": link["mac"], "ip": link["ip"]}
            if privacy.is_private(eid, self.show):
                dropped += 1
                continue
            if len(states) >= MAX_ENTITIES:
                continue
            states[eid] = [clip(s.get("state")), clip(attrs.get("unit_of_measurement"), 32), clip(attrs.get("friendly_name"))]
            if privacy.domain_of(eid) in ITEM_DOMAINS:
                items[eid] = attrs
        self._states, self._trackers = states, trackers
        self.private_dropped += dropped
        old = self._items
        self._items = {}
        for eid, attrs in items.items():
            self._note_item(eid, states[eid][0], attrs)
        for eid in set(old) - set(self._items):
            self._refs.pop(eid, None)
            self._related.pop(eid, None)

    async def _registry(self, conn: _Conn, type_: str):
        if type_ in self._dead_types:
            return None
        try:
            result = await self._request(conn, type_)
        except SourceError as exc:
            if exc.key in ("source.hub.forbidden", "source.hub.unknown_command"):
                return None              # token without admin rights (or an older hub): registry not readable
            raise
        return result if isinstance(result, list) else []

    async def snapshot(self, conn: _Conn) -> dict:
        self._store_states(await self._request(conn, "get_states"))
        devices_raw = await self._registry(conn, "config/device_registry/list")
        entities_raw = await self._registry(conn, "config/entity_registry/list")
        areas_raw = await self._registry(conn, "config/area_registry/list")
        entries_raw = await self._registry(conn, "config_entries/get")
        snap = build_snapshot(self._states, devices_raw, entities_raw, areas_raw, entries_raw=entries_raw,
                              trackers=self._trackers, items=self._items, show=self.show)
        snap["admin"] = self.admin_mode
        return snap

    # ------------------------------------------------------------ admin mode: dependencies and traces
    def want_traces(self, entity_id: str) -> int:
        """Ask for the last runs of one automation/script (admin mode). Returns the place in the queue (1 = next),
        0 when admin mode is off or the item is unknown."""
        if not self.admin_mode or entity_id not in self._items or not self._items[entity_id].get("id"):
            return 0
        if entity_id not in self._trace_wanted and len(self._trace_wanted) >= MAX_TRACE_WANTED:
            return 0
        self._trace_wanted[entity_id] = True
        return list(self._trace_wanted).index(entity_id) + 1

    def _known(self) -> set:
        return set(self._states)

    def _plan_lookups(self, wall: float) -> tuple[list, list]:
        """(configuration reads due, related lookups due) - fresh answers, blocked items and loops are left out."""
        configs, related = [], []
        in_loop = {e for comp in self._cycles for e in comp}
        for eid, item in sorted(self._items.items()):
            dom = privacy.domain_of(eid)
            type_ = dom + "/config"
            refs = self._refs.get(eid)
            fp = refs.get("fp") if refs else None
            if type_ not in self._dead_types and not self._blocks.blocked("c:" + eid, wall) and wall >= self._due_config.get(eid, 0.0):
                configs.append(eid)
            if "search/related" in self._dead_types or eid in in_loop or self._blocks.blocked("r:" + eid, wall, fp):
                continue
            if refs and any(e in in_loop for e in refs.get("action", ())):
                continue                      # it calls a script in a loop: the hub would recurse there too
            rel = self._related.get(eid)
            if rel is None or wall >= self._due_related.get(eid, 0.0) or (fp and rel.get("fp") != fp):
                if refs or type_ in self._dead_types or self._blocks.blocked("c:" + eid, wall):
                    related.append(eid)       # only after the configuration is known (or cannot be known)
        return configs, related

    async def _read_config(self, conn: _Conn, eid: str, wall: float) -> None:
        dom = privacy.domain_of(eid)
        try:
            result = await self._request(conn, "automation/config" if dom == "automation" else "script/config", entity_id=eid)
        except SourceError as exc:
            if exc.key != "source.hub.unknown_command":
                self._blocks.fail("c:" + eid, wall, exc.key, timeout=exc.key == "source.hub.timeout")
            self._due_config[eid] = wall + deps.jitter(CONFIG_FRESH_S)
            return
        cfg = result.get("config") if isinstance(result, dict) and isinstance(result.get("config"), dict) else result
        refs = deps.refs_from_config(cfg if isinstance(cfg, dict) else {}, self._known())
        refs["fp"] = deps.fingerprint(cfg)
        refs["t"] = now()
        old = self._refs.get(eid)
        if not old or old.get("fp") != refs["fp"]:
            self._deps_dirty = True
        self._refs[eid] = refs
        self._blocks.ok("c:" + eid)
        self._due_config[eid] = wall + deps.jitter(CONFIG_FRESH_S)
        self._cycles = deps.cycles(deps.script_calls({e: r for e, r in self._refs.items() if e.startswith("script.")}))

    async def _lookup_related(self, conn: _Conn, eid: str, wall: float) -> None:
        fp = (self._refs.get(eid) or {}).get("fp")
        try:
            result = await self._request(conn, "search/related", item_type=privacy.domain_of(eid), item_id=eid)
        except SourceError as exc:
            if exc.key != "source.hub.unknown_command":
                self._blocks.fail("r:" + eid, wall, exc.key, fp=fp, timeout=exc.key == "source.hub.timeout")
            self._due_related[eid] = wall + deps.jitter(RELATED_FRESH_S)
            self._deps_dirty = True
            return
        ids = [e for e in deps.related_ids(result) if not privacy.is_private(e, self.show) and e != eid]
        self._related[eid] = {"ids": ids, "t": now(), "fp": fp}
        self._blocks.ok("r:" + eid)
        self._due_related[eid] = wall + deps.jitter(RELATED_FRESH_S)
        self._deps_dirty = True

    async def _read_traces(self, conn: _Conn, eid: str) -> dict | None:
        item = self._items.get(eid) or {}
        dom = privacy.domain_of(eid)
        item_id = item.get("id")
        if not item_id or not ITEM_ID_RX.match(item_id):
            return None
        runs_raw = await self._request(conn, "trace/list", domain=dom, item_id=item_id)
        runs = []
        for r in runs_raw if isinstance(runs_raw, list) else []:
            if not isinstance(r, dict) or not isinstance(r.get("run_id"), str) or not RUN_ID_RX.match(r["run_id"]):
                continue
            stamp = r.get("timestamp") if isinstance(r.get("timestamp"), dict) else {}
            last = r.get("last_step")
            runs.append({"run_id": r["run_id"], "start": _ts(stamp.get("start")), "finish": _ts(stamp.get("finish")),
                         "state": clip(r.get("state"), 24), "execution": clip(r.get("script_execution"), 32),
                         "last_step": last if isinstance(last, str) and STEP_RX.match(last) else None,
                         "error": bool(r.get("error"))})     # the error text may name people or values: only yes/no
        runs.sort(key=lambda r: r["start"] or 0, reverse=True)
        runs = runs[:MAX_TRACE_RUNS]
        steps = []
        if runs and self._budgets["trace"].take(self._clock()):
            raw = await self._request(conn, "trace/get", domain=dom, item_id=item_id, run_id=runs[0]["run_id"])
            trace = raw.get("trace") if isinstance(raw, dict) and isinstance(raw.get("trace"), dict) else {}
            for path, entries in trace.items():
                if not isinstance(path, str) or not STEP_RX.match(path):
                    continue
                for e in entries if isinstance(entries, list) else []:
                    if isinstance(e, dict):
                        steps.append({"path": path, "t": _ts(e.get("timestamp")), "ok": not e.get("error")})
            steps.sort(key=lambda s: s["t"] or 0)
            steps = steps[:MAX_TRACE_STEPS]
        return {"type": "hub_traces", "source": SOURCE, "t": now(), "entity_id": eid, "runs": runs, "steps": steps}

    def deps_frame(self) -> dict:
        wall = time.time()
        autos, scripts = {}, {}
        for eid in self._items:
            refs = self._refs.get(eid)
            rel = self._related.get(eid)
            if not refs and not rel:
                continue
            entry = {r: list((refs or {}).get(r, ())) for r in deps.ROLES}
            entry["text"] = list((refs or {}).get("text", ()))
            entry["related"] = list((rel or {}).get("ids", ()))
            (autos if eid.startswith("automation.") else scripts)[eid] = entry
        return {"type": "hub_deps", "source": SOURCE, "t": now(), "automations": autos, "scripts": scripts,
                "cycles": [list(c) for c in self._cycles],
                "blocked": [dict(b, kind="config" if b["key"].startswith("c:") else "related", entity_id=b["key"][2:])
                            for b in self._blocks.active(wall)]}

    async def _deps_loop(self, conn: _Conn, emit) -> None:
        """Admin mode: one lookup at a time, each within its budget; requested traces first."""
        last_frame = self._clock()
        while True:
            mono, wall = self._clock(), time.time()
            did = False
            if self._trace_wanted and "trace/list" not in self._dead_types and self._budgets["trace"].take(mono):
                eid = next(iter(self._trace_wanted))
                self._trace_wanted.pop(eid, None)
                try:
                    frame = await self._read_traces(conn, eid)
                except SourceError as exc:
                    frame = {"type": "hub_traces", "source": SOURCE, "t": now(), "entity_id": eid, "runs": [], "steps": [],
                             "error": exc.key}
                if frame:
                    await emit_event(emit, frame)
                did = True
            else:
                configs, related = self._plan_lookups(wall)
                if configs and self._budgets["config"].take(mono):
                    await self._read_config(conn, configs[0], wall)
                    did = True
                elif related and self._budgets["related"].take(mono):
                    await self._lookup_related(conn, related[0], wall)
                    did = True
            if self._deps_dirty and (mono - last_frame >= DEPS_FRAME_S or not did):
                self._deps_dirty = False
                last_frame = mono
                await emit_event(emit, self.deps_frame())
            await asyncio.sleep(0.05 if did else 0.5)

    # ------------------------------------------------------------ run / test
    async def _session(self, emit, status: StatusTracker) -> None:
        conn = await self._open()
        worker = None
        try:
            conn.start(self._on_event)
            await self._request(conn, "subscribe_events", event_type="state_changed")
            await self._request(conn, "subscribe_events", event_type="automation_triggered")
            await self._request(conn, "subscribe_events", event_type="script_started")
            await emit_event(emit, await self.snapshot(conn))
            await status.set(emit, True, "source.hub.ok")
            if self.admin_mode:
                worker = asyncio.create_task(self._deps_loop(conn, emit))
            start = self._clock()
            next_snapshot = start + self.snapshot_interval
            next_triggers = start + TRIGGER_FRAME_S
            next_bridge = start
            last_ping = start
            while True:
                waits = {conn.reader} | ({worker} if worker else set())
                done, _ = await asyncio.wait(waits, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
                if conn.reader in done:
                    exc = conn.reader.exception()
                    if isinstance(exc, (WsError, SourceError)) or exc is None:
                        raise SourceError("source.hub.closed", "connection closed")
                    raise exc
                if worker is not None and worker in done:
                    exc = worker.exception()
                    raise exc if exc else SourceError("source.hub.closed", "connection closed")
                for ev in self.take_states():
                    await emit_event(emit, ev)
                mono = self._clock()
                if mono >= next_triggers:
                    next_triggers = mono + TRIGGER_FRAME_S
                    frame = self.take_triggers()
                    if frame:
                        await emit_event(emit, frame)
                if mono >= next_bridge:
                    next_bridge = mono + BRIDGE_FRAME_S
                    await emit_event(emit, self.bridge_frame())
                if mono >= next_snapshot:
                    next_snapshot = mono + self.snapshot_interval
                    await emit_event(emit, await self.snapshot(conn))
                if mono - last_ping >= HEARTBEAT_S:
                    await conn.ws.ping()
                    last_ping = mono
                if time.monotonic() - conn.ws.last_rx > SILENCE_S:
                    raise SourceError("source.hub.timeout", "hub silent")
        finally:
            if worker is not None:
                worker.cancel()
                try:
                    await worker
                except (asyncio.CancelledError, Exception):
                    pass
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


def build_snapshot(states: dict, devices_raw, entities_raw, areas_raw, entries_raw=None, trackers=None, items=None,
                   show=frozenset()) -> dict:
    """Registry lists + states -> hub_snapshot event (only the fields the map needs; private domains left out)."""
    areas = []
    for a in (areas_raw or [])[:MAX_AREAS]:
        if isinstance(a, dict) and a.get("area_id"):
            areas.append({"id": clip(a.get("area_id"), 64), "name": clip(a.get("name"))})
    entry_domain = {}
    for e in entries_raw or []:
        if isinstance(e, dict) and isinstance(e.get("entry_id"), str) and isinstance(e.get("domain"), str):
            entry_domain[e["entry_id"][:64]] = clip(e["domain"], 64)
    devices, device_area = [], {}
    for d in devices_raw or []:
        if len(devices) >= MAX_DEVICES:
            break
        if not isinstance(d, dict) or not d.get("id") or d.get("disabled_by"):
            continue
        did = clip(d.get("id"), 64)
        device_area[did] = clip(d.get("area_id"), 64)
        doms = []
        for entry in d.get("config_entries") or []:
            dom = entry_domain.get(entry) if isinstance(entry, str) else None
            if dom and dom not in doms:
                doms.append(dom)
        devices.append({"id": did, "name": clip(d.get("name_by_user") or d.get("name")),
                        "model": clip(d.get("model")), "manufacturer": clip(d.get("manufacturer")),
                        "area": device_area[did], "via": clip(d.get("via_device_id"), 64),
                        "service": d.get("entry_type") == "service", "macs": privacy.device_macs(d.get("connections")),
                        "sw": clip(d.get("sw_version"), 64), "domains": doms[:8]})
    entities, seen = [], set()
    for e in entities_raw or []:
        if len(entities) >= MAX_ENTITIES:
            break
        if not isinstance(e, dict) or not isinstance(e.get("entity_id"), str) or e.get("disabled_by"):
            continue
        eid = e["entity_id"][:255]
        seen.add(eid)
        if privacy.is_private(eid, show):
            continue
        st = states.get(eid) or [None, None, None]
        did = clip(e.get("device_id"), 64)
        entities.append({"entity_id": eid, "device_id": did,
                         "name": clip(e.get("name") or e.get("original_name") or st[2]),
                         "state": st[0], "unit": st[1], "domain": eid.split(".", 1)[0],
                         "area": clip(e.get("area_id"), 64) or device_area.get(did)})
    for eid, st in states.items():          # entities without registry entry (or registry not readable)
        if len(entities) >= MAX_ENTITIES:
            break
        if eid in seen or privacy.is_private(eid, show):
            continue
        entities.append({"entity_id": eid, "device_id": None, "name": st[2], "state": st[0], "unit": st[1],
                         "domain": eid.split(".", 1)[0], "area": None})
    snap = {"type": "hub_snapshot", "source": SOURCE, "t": now(), "devices": devices, "entities": entities,
            "areas": areas,
            # hardware/IP addresses of trackers for matching devices; the tracker's entity id may name a person: left out
            "trackers": sorted(({"mac": v.get("mac"), "ip": v.get("ip")} for v in (trackers or {}).values()),
                               key=lambda x: (x["mac"] or "", x["ip"] or ""))[:MAX_ENTITIES],
            "automations": [dict(v, entity_id=k) for k, v in sorted((items or {}).items()) if k.startswith("automation.")],
            "scripts": [dict(v, entity_id=k) for k, v in sorted((items or {}).items()) if k.startswith("script.")]}
    if devices_raw is None or entities_raw is None:
        snap["limited"] = True               # registries not readable with this token
    return snap
