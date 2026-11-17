"""Hub bridge 2: admin mode against a fake hub, privacy filter, load protection and a load test.

The fake hub answers configuration, trace and related-item requests, sends automation runs and state changes with
contexts, and holds private entities (a person, a tracker with coordinates, a zone, a camera, a calendar). It records
every message type and refuses to be asked about scripts that call each other in a loop (the real hub would recurse
until its limit)."""
import asyncio
import json
import random
import time

import pytest

from lanloom.sources import hub as m
from lanloom.sources import hub_deps as deps
from test_sources_hub import TOKEN, FakeHub

T = "2026-11-16T07:00:00.000000+00:00"
STATES = [
    {"entity_id": "automation.hall", "state": "on", "attributes": {"id": "1001", "mode": "single", "last_triggered": T,
                                                                     "friendly_name": "Hall light"}},
    {"entity_id": "automation.loopy", "state": "on", "attributes": {"id": "1002", "mode": "queued"}},
    {"entity_id": "automation.broken", "state": "on", "attributes": {"id": "1003", "mode": "single"}},
    {"entity_id": "script.a", "state": "off", "attributes": {"mode": "single"}},
    {"entity_id": "script.b", "state": "off", "attributes": {"mode": "single"}},
    {"entity_id": "script.self", "state": "off", "attributes": {"mode": "single"}},
    {"entity_id": "light.hall", "state": "off", "attributes": {"friendly_name": "Hall"}},
    {"entity_id": "binary_sensor.motion", "state": "off", "attributes": {}},
    {"entity_id": "sensor.power", "state": "0", "attributes": {"unit_of_measurement": "W"}},
    {"entity_id": "person.alice", "state": "home", "attributes": {"friendly_name": "Alice", "latitude": 51.1, "longitude": 13.7}},
    {"entity_id": "device_tracker.alice_phone", "state": "home",
     "attributes": {"mac": "AA-BB-CC-00-11-22", "ip": "192.168.1.50", "latitude": 51.1, "longitude": 13.7, "host_name": "alice"}},
    {"entity_id": "zone.home", "state": "1", "attributes": {"latitude": 51.1}},
    {"entity_id": "camera.door", "state": "idle", "attributes": {"access_token": "camtok", "entity_picture": "/api/camera_proxy/x"}},
    {"entity_id": "calendar.family", "state": "on", "attributes": {"message": "Dentist Alice"}},
]
CONFIGS = {
    "automation.hall": {"id": "1001", "alias": "Hall light",
                        "trigger": [{"platform": "state", "entity_id": "binary_sensor.motion"}],
                        "condition": [{"condition": "template", "value_template": "{{ is_state('sensor.power', '0') }}"}],
                        "action": [{"service": "light.turn_on", "target": {"entity_id": "light.hall"}}]},
    "automation.loopy": {"id": "1002", "trigger": [], "action": [{"service": "script.a"}]},
    "script.a": {"sequence": [{"service": "script.b"}, {"service": "light.turn_off", "entity_id": "light.hall"}]},
    "script.b": {"sequence": [{"action": "script.turn_on", "target": {"entity_id": "script.a"}}]},
    "script.self": {"sequence": [{"service": "logbook.log", "data": {"entity_id": "script.self", "message": "x"}}]},
}
IN_LOOP = {"script.a", "script.b", "script.self", "automation.loopy"}


class AdminHub(FakeHub):
    """The fake hub with automations, scripts, traces, related items, contexts and private entities."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.asked = []            # (type, item) of every admin request
        self.recursion = []        # related lookups the real hub would have recursed on
        self.unknown = set()       # message types answered with "unknown command"
        self.events_sent = False

    async def reply(self, writer, msg):
        t = msg["type"]
        if t == "auth":
            return await super().reply(writer, msg)
        mid = msg["id"]

        async def ok(result):
            await self.send(writer, {"id": mid, "type": "result", "success": True, "result": result})

        async def fail(code):
            await self.send(writer, {"id": mid, "type": "result", "success": False, "error": {"code": code, "message": code}})

        if t in self.unknown:
            return await fail("unknown_command")
        if t == "get_states":
            return await ok(STATES)
        if t == "config/device_registry/list":
            return await ok([{"id": "d1", "name": "Hall lamp", "area_id": "hall", "config_entries": ["e1"], "sw_version": "1.2",
                              "connections": [["mac", "AA:BB:CC:00:11:33"], ["radio", "0x00124b"]]}])
        if t == "config/entity_registry/list":
            return await ok([{"entity_id": "light.hall", "device_id": "d1"}, {"entity_id": "person.alice", "device_id": None}])
        if t == "config/area_registry/list":
            return await ok([{"area_id": "hall", "name": "Hall"}])
        if t == "config_entries/get":
            return await ok([{"entry_id": "e1", "domain": "sample_bridge", "title": "Alice's bridge", "state": "loaded"}])
        if t in ("automation/config", "script/config"):
            eid = msg["entity_id"]
            self.asked.append((t, eid))
            if eid == "automation.broken":
                return await fail("unknown_error")
            return await ok({"config": CONFIGS.get(eid, {})})
        if t == "search/related":
            eid = msg["item_id"]
            self.asked.append((t, eid))
            if eid in IN_LOOP:
                self.recursion.append(eid)
                return await fail("unknown_error")
            return await ok({"entity": ["light.hall", "person.alice", "binary_sensor.motion"], "automation": [eid],
                             "area": ["hall"], "device": ["d1"]})
        if t == "trace/list":
            self.asked.append((t, msg["item_id"]))
            return await ok([{"run_id": "abc123", "state": "stopped", "script_execution": "finished", "last_step": "action/0",
                              "error": "Alice was at home", "trigger": "state of person.alice",
                              "timestamp": {"start": T, "finish": T}}])
        if t == "trace/get":
            self.asked.append((t, msg["run_id"]))
            return await ok({"trace": {
                "trigger/0": [{"path": "trigger/0", "timestamp": T, "changed_variables": {"trigger": {"entity_id": "person.alice"}}}],
                "action/0": [{"path": "action/0", "timestamp": T, "result": {"params": {"service_data": {"secret": "s3"}}}}],
                "bad path!": [{"timestamp": T}]}})
        if t == "subscribe_events":
            await ok(None)
            if msg["event_type"] == "script_started" and not self.events_sent:
                self.events_sent = True
                await self.events(writer, mid)
            return
        await ok({"version": "2026.11.0"})

    async def events(self, writer, mid):
        def ev(event_type, data, context):
            return {"id": mid, "type": "event", "event": {"event_type": event_type, "data": data, "context": context}}
        for e in [
            ev("automation_triggered", {"name": "Hall light", "entity_id": "automation.hall", "source": "state of person.alice"},
               {"id": "ctx-run-1", "parent_id": "ctx-trigger", "user_id": None}),
            ev("state_changed", {"entity_id": "light.hall", "old_state": {"state": "off"},
                                 "new_state": {"state": "on", "attributes": {"brightness": 200}, "context": {"id": "ctx-run-1"}}}, {}),
            ev("state_changed", {"entity_id": "sensor.power", "old_state": {"state": "0"},
                                 "new_state": {"state": "40", "attributes": {}, "context": {"id": "c2", "user_id": "u-alice-123"}}}, {}),
            ev("state_changed", {"entity_id": "person.alice", "old_state": {"state": "home"},
                                 "new_state": {"state": "not_home", "attributes": {"latitude": 52.0}, "context": {"id": "c3"}}}, {}),
            ev("state_changed", {"entity_id": "device_tracker.alice_phone", "old_state": {"state": "home"},
                                 "new_state": {"state": "not_home", "attributes": {"mac": "aa:bb:cc:00:11:22", "latitude": 52.0},
                                               "context": {"id": "c4"}}}, {}),
            ev("state_changed", {"entity_id": "calendar.family", "old_state": {"state": "off"},
                                 "new_state": {"state": "on", "attributes": {"message": "Dentist Alice"}}}, {}),
            ev("call_service", {"domain": "light", "service": "turn_on", "service_data": {"entity_id": "light.hall"}}, {}),
            ev("script_started", {"name": "A", "entity_id": "script.a"}, {"id": "ctx-run-2"}),
        ]:
            await self.send(writer, e)


def run(coro):
    return asyncio.run(coro)


async def _drive(srv, hub, until, timeout=20.0, after_snapshot=None):
    events = []
    task = asyncio.create_task(hub.run(events.append))
    t0 = time.monotonic()
    fired = False
    try:
        while time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.05)
            if not fired and after_snapshot and any(e["type"] == "hub_snapshot" for e in events):
                fired = True
                after_snapshot(hub)
            if until(events):
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await srv.stop()
    return events


def _admin_run():
    async def go():
        srv = await AdminHub().start()
        hub = m.Hub("http://127.0.0.1:%d" % srv.port, TOKEN, admin_mode=True)
        events = await _drive(srv, hub, lambda evs: any(e["type"] == "hub_traces" for e in evs)
                              and any(e["type"] == "hub_deps" and e["cycles"] for e in evs)
                              and any(e["type"] == "hub_state" and e["entity_id"] == "sensor.power" for e in evs),
                              after_snapshot=lambda h: h.want_traces("automation.hall"))
        return srv, hub, events
    return run(go())


@pytest.fixture(scope="module")
def admin_run():
    return _admin_run()


# ================================================================== admin mode end to end
def test_admin_mode_only_sends_allowed_messages_and_never_asks_about_loops(admin_run):
    srv, hub, events = admin_run
    assert set(srv.received) <= m.ALLOWED_TYPES
    assert srv.recursion == []                                     # loops never looked up
    related = [i for t, i in srv.asked if t == "search/related"]
    assert "automation.hall" in related and not set(related) & IN_LOOP
    configs = [i for t, i in srv.asked if t.endswith("/config")]
    assert sorted(configs) == sorted({"automation.hall", "automation.loopy", "automation.broken", "script.a", "script.b",
                                      "script.self"})            # every item exactly once, nothing asked twice
    assert all(json.loads(r)["type"] != "render_template" for r in srv.raw)
    assert hub.refused == 0


def test_dependencies_keep_only_entity_ids_per_role(admin_run):
    _srv, _hub, events = admin_run
    frame = [e for e in events if e["type"] == "hub_deps"][-1]
    hall = frame["automations"]["automation.hall"]
    assert hall["trigger"] == ["binary_sensor.motion"] and hall["action"] == ["light.hall"]
    assert hall["text"] == ["sensor.power"] and hall["condition"] == []       # only found in a template: weaker
    assert "person.alice" not in hall["related"] and "light.hall" in hall["related"]
    assert frame["cycles"] == [["script.a", "script.b"], ["script.self"]]
    assert [(b["entity_id"], b["kind"]) for b in frame["blocked"]] == [("automation.broken", "config")]
    # the flow keeps the structure only: kinds, entity ids, the name of the action called - no alias, template or data
    assert hall["flow"] == {"trigger": [{"k": "state", "e": ["binary_sensor.motion"]}], "condition": [{"k": "tpl", "tpl": True}],
                            "action": [{"k": "call", "svc": "light.turn_on", "e": ["light.hall"]}]}
    assert frame["scripts"]["script.self"]["flow"]["action"] == [{"k": "call", "svc": "logbook.log", "e": ["script.self"]}]
    blob = json.dumps(frame)
    for word in ("value_template", "is_state", "message", "alias", "Hall light", "\"x\""):
        assert word not in blob


def test_traces_on_request_keep_no_variables_or_error_texts(admin_run):
    srv, _hub, events = admin_run
    frame = [e for e in events if e["type"] == "hub_traces"][0]
    assert frame["entity_id"] == "automation.hall"
    assert frame["runs"] == [{"run_id": "abc123", "start": frame["runs"][0]["start"], "finish": frame["runs"][0]["finish"],
                              "state": "stopped", "execution": "finished", "last_step": "action/0", "error": True}]
    assert [s["path"] for s in frame["steps"]] == ["trigger/0", "action/0"] or \
        sorted(s["path"] for s in frame["steps"]) == ["action/0", "trigger/0"]
    blob = json.dumps(frame)
    for word in ("Alice", "person.alice", "service_data", "s3", "changed_variables", "bad path"):
        assert word not in blob
    assert [t for t, _i in srv.asked if t.startswith("trace/")] == ["trace/list", "trace/get"]


def test_privacy_filter_on_snapshot_and_events(admin_run):
    _srv, hub, events = admin_run
    snap = [e for e in events if e["type"] == "hub_snapshot"][0]
    ids = {e["entity_id"] for e in snap["entities"]}
    assert not {i for i in ids if i.split(".")[0] in ("person", "device_tracker", "zone", "camera", "calendar")}
    assert snap["trackers"] == [{"mac": "aa:bb:cc:00:11:22", "ip": "192.168.1.50"}]
    assert snap["devices"][0]["macs"] == ["aa:bb:cc:00:11:33"] and snap["devices"][0]["domains"] == ["sample_bridge"]
    assert {a["entity_id"]: a["id"] for a in snap["automations"]} == {"automation.hall": "1001", "automation.loopy": "1002",
                                                                       "automation.broken": "1003"}
    states = {e["entity_id"]: e for e in events if e["type"] == "hub_state"}
    assert states["light.hall"]["by"] == "automation.hall"          # caused by the automation run we saw
    assert states["sensor.power"]["by"] == "user"                   # a person did it: only the word, never the id
    assert not {"person.alice", "device_tracker.alice_phone", "calendar.family"} & set(states)
    for e in states.values():
        assert set(e) <= {"type", "source", "entity_id", "state", "t", "by"}
    blob = json.dumps(events)
    for word in ("u-alice-123", "ctx-run-1", "ctx-trigger", "latitude", "Dentist", "camtok", "camera_proxy", "Alice's bridge",
                 "service_data", "alice_phone", "host_name"):
        assert word not in blob, word
    bridge = [e for e in events if e["type"] == "hub_bridge"][-1]
    assert bridge["private_dropped"] >= 5 and bridge["refused"] == 0 and bridge["admin"] is True


def test_runs_are_counted_per_automation_and_script(admin_run):
    _srv, hub, events = admin_run
    frames = [e for e in events if e["type"] == "hub_triggers"]
    total = {}
    for f in frames:
        for eid, r in f["runs"].items():
            total[eid] = total.get(eid, 0) + r["n"]
    pending = {e: r[0] for e, r in hub._runs.items() if r[0]}
    for eid, n in pending.items():
        total[eid] = total.get(eid, 0) + n
    assert total == {"automation.hall": 1, "script.a": 1}


def test_basic_mode_never_sends_admin_types():
    async def go():
        srv = await AdminHub().start()
        hub = m.Hub("http://127.0.0.1:%d" % srv.port, TOKEN)
        events = await _drive(srv, hub, lambda evs: any(e["type"] == "hub_state" and e["entity_id"] == "sensor.power" for e in evs))
        return srv, hub, events
    srv, hub, events = run(go())
    assert not set(srv.received) & m.ADMIN_TYPES
    assert hub.want_traces("automation.hall") == 0
    assert not [e for e in events if e["type"] in ("hub_deps", "hub_traces")]
    assert srv.received.count("subscribe_events") == 3


def test_unknown_command_is_not_sent_again():
    async def go():
        srv = await AdminHub().start()
        srv.unknown = {"search/related", "config_entries/get"}
        hub = m.Hub("http://127.0.0.1:%d" % srv.port, TOKEN, admin_mode=True, snapshot_interval=30)
        events = await _drive(srv, hub, lambda evs: any(e["type"] == "hub_deps" and e["cycles"] for e in evs), timeout=10)
        return srv, hub, events
    srv, hub, events = run(go())
    assert srv.received.count("search/related") == 1 and srv.received.count("config_entries/get") == 1
    assert hub.bridge_frame()["unknown_types"] == ["config_entries/get", "search/related"]


# ================================================================== pure parts
def test_refs_from_config_roles_and_templates():
    known = {"binary_sensor.motion", "light.hall", "sensor.power", "script.a", "switch.fan", "sensor.t"}
    cfg = {"triggers": [{"trigger": "state", "entity_id": ["binary_sensor.motion", "sensor.unknown"]}],
           "conditions": [{"condition": "state", "entity_id": "sensor.t", "state": "on"}],
           "actions": [{"choose": [{"conditions": [{"condition": "state", "entity_id": "switch.fan"}],
                                    "sequence": [{"action": "script.a"}]}]},
                       {"service": "light.turn_on", "data": {"entity_id": "light.hall, sensor.power"}},
                       {"delay": "{{ states('sensor.power') | int }}"}]}
    refs = deps.refs_from_config(cfg, known)
    assert refs["trigger"] == ["binary_sensor.motion"] and refs["condition"] == ["sensor.t"]
    assert refs["action"] == ["light.hall", "script.a", "sensor.power", "switch.fan"]
    assert refs["text"] == []                                         # sensor.power already a direct reference
    deep = {"action": [{"x": [{"y": {"z": None}}]}]}
    for _ in range(200):
        deep = {"action": [deep]}
    assert deps.refs_from_config(deep, known)["action"] == []        # depth-capped, no recursion error


def test_cycles_tarjan():
    graph = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"d"}, "e": {"a"}, "f": set()}
    assert deps.cycles(graph) == [["a", "b", "c"], ["d"]]
    chain = {"n%d" % i: {"n%d" % (i + 1)} for i in range(5000)}
    chain["n5000"] = {"n0"}
    assert len(deps.cycles(chain)[0]) == 5001                          # iterative: no recursion limit


def test_budget_tokens_per_minute():
    b = deps.Budget(6, burst=2, mono=0.0)
    assert [b.take(0.0) for _ in range(3)] == [True, True, False]
    assert b.take(5.0) is False and b.take(10.0) is True               # one token every 10 s
    assert b.wait_s(10.0) == pytest.approx(10.0)
    assert b.waits == 2


def test_blocklist_backs_off_and_a_changed_configuration_lifts_it():
    bl = deps.Blocklist()
    assert bl.fail("r:x", 0.0, "e", fp="f1") and bl.blocked("r:x", 1.0, "f1")
    assert not bl.blocked("r:x", deps.BLOCK_S + 1, "f1")
    bl.fail("r:x", 0.0, "e", fp="f1")
    assert bl.items["r:x"]["until"] == 2 * deps.BLOCK_S
    for _ in range(10):
        bl.fail("r:x", 0.0, "e", fp="f1")
    assert bl.items["r:x"]["until"] == deps.BLOCK_MAX_S                # at most a week
    assert not bl.blocked("r:x", 1.0, "f2") and "r:x" not in bl.items  # changed configuration: ask again
    assert not bl.fail("c:y", 0.0, timeout=True) and not bl.fail("c:y", 0.0, timeout=True)
    assert bl.fail("c:y", 0.0, timeout=True) and bl.blocked("c:y", 1.0)   # three timeouts count as one error
    bl.ok("c:y")
    assert not bl.blocked("c:y", 1.0)


# ================================================================== load test
class Instant:
    """A connection stand-in: answers every request at once (configurations of a big installation)."""

    def __init__(self, hub, n_scripts):
        self.hub = hub
        self.log = []
        self.n_scripts = n_scripts

    async def request(self, conn, type_, **fields):
        await self.hub._send(conn, type_, **fields)          # through the real send path (allow-list, admin check)
        self.log.append((self.hub._clock(), type_))
        if type_.endswith("/config"):
            eid = fields["entity_id"]
            if eid.startswith("script."):
                i = int(eid.rsplit("_", 1)[1])
                return {"config": {"sequence": [{"service": "script.s_%d" % ((i + 1) % self.n_scripts)}]}}
            return {"config": {"trigger": [{"entity_id": "light.l_1"}], "action": [{"service": "script.s_%d" % (int(eid.rsplit("_", 1)[1]) % 50)}]}}
        if type_ == "trace/list":
            return []
        return {"entity": ["light.l_1"]}


class Sink:
    async def send_text(self, text):
        pass


def test_load_budgets_hold_for_a_big_installation():
    """3000 automations and 300 scripts, simulated 15 minutes of the admin worker: every budget holds in every minute,
    nothing in the script loop is ever looked up, and traces stay within their budget even when asked for constantly."""
    clock = [0.0]
    hub = m.Hub("http://127.0.0.1:1", TOKEN, admin_mode=True, clock=lambda: clock[0])
    n_auto, n_scripts = 3000, 300
    states = [{"entity_id": "automation.a_%d" % i, "state": "on", "attributes": {"id": str(10000 + i)}} for i in range(n_auto)]
    states += [{"entity_id": "script.s_%d" % i, "state": "off", "attributes": {}} for i in range(n_scripts)]
    states += [{"entity_id": "light.l_1", "state": "on", "attributes": {}}]
    hub._store_states(states)
    inst = Instant(hub, n_scripts)
    hub._request = inst.request
    conn = m._Conn(Sink())
    emitted = []

    async def emit(ev):
        emitted.append(ev)

    async def go():
        step = 0.05
        while clock[0] < 900.0:
            if int(clock[0] * 20) % 40 == 0:
                hub.want_traces("automation.a_%d" % random.randrange(n_auto))   # someone keeps asking for runs
            await hub._deps_step(conn, emit, clock[0], 1_800_000_000.0 + clock[0])
            clock[0] += step
    run(go())
    per = {}
    for t, type_ in inst.log:
        kind = "config" if type_.endswith("/config") else "related" if type_ == "search/related" else "trace"
        per.setdefault(kind, []).append(t)
    limits = {"config": m.CONFIG_PER_MIN * 2, "related": m.RELATED_PER_MIN * 2, "trace": m.TRACE_PER_MIN + 2}
    for kind, times in per.items():
        for start in range(0, 900, 10):                        # every 60-s window: burst + rate at most
            n = sum(1 for t in times if start <= t < start + 60)
            assert n <= limits[kind], (kind, start, n)
    assert len(per["config"]) >= m.CONFIG_PER_MIN * 14         # but the budget is used, not starved
    assert hub._cycles and len(hub._cycles[0]) == n_scripts      # the whole script ring is one loop
    asked_related = {i for i in hub._related}
    assert not asked_related & {"script.s_%d" % i for i in range(n_scripts)}
    assert hub.refused == 0
    assert all(ty in m.ALLOWED_TYPES for _t, ty in inst.log)


def test_load_event_flood_stays_bounded_and_fast():
    """A flood of state changes and automation runs: memory stays capped, at most STATE_RATE state events per second
    leave the bridge, and the filter costs little per event."""
    clock = [0.0]
    hub = m.Hub("http://127.0.0.1:1", TOKEN, clock=lambda: clock[0])
    n = 120_000
    t0 = time.process_time()
    for i in range(n):
        eid = "sensor.s_%d" % (i % 30_000) if i % 4 else "person.p_%d" % (i % 50)
        hub._on_event({"event": {"event_type": "state_changed", "data": {
            "entity_id": eid, "old_state": {"state": "1"},
            "new_state": {"state": str(i), "attributes": {"unit_of_measurement": "W"}, "context": {"id": "c%d" % i}}}}})
        if i % 3 == 0:
            hub._on_event({"event": {"event_type": "automation_triggered", "data": {"entity_id": "automation.a_%d" % (i % 6000)},
                                     "context": {"id": "run%d" % i}}})
    cpu = time.process_time() - t0
    assert len(hub._pending) <= m.MAX_PENDING and len(hub._states) <= m.MAX_ENTITIES
    assert len(hub._ctx) <= m.MAX_CONTEXTS and len(hub._runs) <= m.MAX_ITEMS
    assert not [e for e in hub._states if e.startswith("person.")] and hub.private_dropped == n // 4
    out = []
    for _ in range(10):
        clock[0] += 1.0
        out.append(len(hub.take_states()))
    assert max(out) <= m.STATE_RATE
    assert cpu / (n * 1.33) < 2e-4, cpu                        # well under 0.2 ms per event even on a small host


def test_a_closed_connection_never_blocks_an_item():
    hub = m.Hub("http://127.0.0.1:1", TOKEN, admin_mode=True)
    hub._store_states([{"entity_id": "automation.x", "state": "on", "attributes": {"id": "1"}}])

    async def closed(conn, type_, **fields):
        raise m.SourceError("source.hub.closed", "connection closed")
    hub._request = closed

    async def go():
        with pytest.raises(m.SourceError):
            await hub._read_config(None, "automation.x", 1_800_000_000.0)
        with pytest.raises(m.SourceError):
            await hub._lookup_related(None, "automation.x", 1_800_000_000.0)
    run(go())
    assert hub._blocks.items == {}
