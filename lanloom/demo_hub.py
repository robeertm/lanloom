"""Demo data of the hub bridge: automations, scripts, run counts, dependencies, a script loop, a blocked lookup,
traces and the bridge counters. Fictional names only; nothing is contacted."""
from __future__ import annotations

import random
import time

from .sources import hub as hub_src

# entity id, name, mode, config id (None: no traces), runs per hour, what it reads / switches
AUTOMATIONS = [
    ("automation.hallway_light_on_motion", "Hallway light on motion", "restart", "1001", 14,
     {"trigger": ["binary_sensor.hallway_4"], "condition": ["sensor.hallway_1"], "action": ["light.hallway_2"]}),
    ("automation.garden_irrigation", "Garden irrigation at dawn", "single", "1002", 0.05,
     {"trigger": [], "condition": ["sensor.garden_1"], "action": ["script.irrigation_cycle"]}),
    ("automation.kitchen_climate", "Kitchen climate", "queued", "1003", 3,
     {"trigger": ["sensor.kitchen_1"], "condition": [], "action": ["switch.kitchen_3"]}),
    ("automation.office_power_saver", "Office power saver", "single", "1004", 0.5,
     {"trigger": ["binary_sensor.office_4"], "condition": [], "action": ["switch.office_3", "light.office_2"]}),
    ("automation.window_open_heating_off", "Window open: heating off", "single", "1005", 1,
     {"trigger": ["binary_sensor.living_room_4", "binary_sensor.bedroom_4"], "condition": [], "action": ["switch.living_room_3"]}),
    ("automation.garage_door_reminder", "Garage door reminder", "single", None, 0.1,
     {"trigger": ["binary_sensor.garage_4"], "condition": [], "action": []}),
    ("automation.living_room_scene_sync", "Living room scene sync", "parallel", "1007", 96,
     {"trigger": ["light.living_room_2"], "condition": [], "action": ["light.living_room_6", "light.living_room_2"]}),
]
SCRIPTS = [
    ("script.irrigation_cycle", "Irrigation cycle", "single", {"action": ["switch.garden_3", "script.irrigation_check"]}),
    ("script.irrigation_check", "Irrigation check", "single", {"action": ["sensor.garden_1", "script.irrigation_cycle"]}),
    ("script.good_night", "Good night", "single", {"action": ["light.living_room_2", "light.kitchen_2", "switch.office_3"]}),
]
LOOP = ["script.irrigation_check", "script.irrigation_cycle"]
BLOCKED = "automation.garage_door_reminder"


def items() -> tuple[list, list]:
    autos = [{"entity_id": e, "id": cid, "mode": mode, "last": None, "state": "on"} for e, _n, mode, cid, _r, _x in AUTOMATIONS]
    scripts = [{"entity_id": e, "id": e.split(".", 1)[1], "mode": mode, "last": None, "state": "off"} for e, _n, mode, _x in SCRIPTS]
    return autos, scripts


def extend_snapshot(snap: dict, now: float) -> dict:
    """Automations/scripts as entities and items, hardware addresses of three devices, tracker count."""
    autos, scripts = items()
    for i, (eid, name, *_rest) in enumerate(AUTOMATIONS):
        autos[i]["last"] = now - 60 * (i * 7 + 3)
        snap["entities"].append({"entity_id": eid, "device_id": None, "name": name, "state": "on", "unit": None,
                                 "domain": "automation", "area": None})
    for eid, name, *_rest in SCRIPTS:
        snap["entities"].append({"entity_id": eid, "device_id": None, "name": name, "state": "off", "unit": None,
                                 "domain": "script", "area": None})
    for n, d in enumerate(snap["devices"][:3]):
        d["macs"] = ["02:00:5e:10:00:%02x" % (0x20 + n)]       # locally administered example addresses
        d["domains"] = ["sample_bridge"]
    snap["automations"], snap["scripts"] = autos, scripts
    snap["trackers"] = [{"mac": "02:00:5e:10:00:%02x" % (0x40 + n), "ip": None} for n in range(4)]
    snap["admin"] = True
    return snap


def triggers_frame(now: float, rng: random.Random, dt: float = 10.0) -> dict | None:
    runs = {}
    for eid, _n, _m, _c, per_hour, _x in AUTOMATIONS:
        expect = per_hour * dt / 3600.0
        n = int(expect) + (1 if rng.random() < expect - int(expect) else 0)
        if n:
            runs[eid] = {"n": n, "last": now - rng.random() * dt}
    return {"type": "hub_triggers", "source": "hub", "t": now, "runs": runs} if runs else None


def history_frames(now: float, rng: random.Random) -> list:
    """Run counts of the last 24 hours (so the bridge card and the nightly report have something to show at once)."""
    frames = []
    for h in range(24, 0, -1):
        t = now - h * 3600 + 1800
        runs = {}
        for eid, _n, _m, _c, per_hour, _x in AUTOMATIONS:
            night = 0.3 if 1 <= time.localtime(t).tm_hour <= 6 else 1.0
            n = int(per_hour * night + (1 if rng.random() < (per_hour * night) % 1 else 0))
            if n:
                runs[eid] = {"n": n, "last": t}
        if runs:
            frames.append({"type": "hub_triggers", "source": "hub", "t": t, "runs": runs})
    return frames


def deps_frame(now: float) -> dict:
    autos = {e: dict({r: list(x.get(r, [])) for r in ("trigger", "condition", "action")}, text=[], related=list(x.get("action", [])))
             for e, _n, _m, _c, _r, x in AUTOMATIONS if e != BLOCKED}
    scripts = {e: {"trigger": [], "condition": [], "action": list(x["action"]), "text": [], "related": []} for e, _n, _m, x in SCRIPTS}
    return {"type": "hub_deps", "source": "hub", "t": now - 240, "automations": autos, "scripts": scripts, "cycles": [list(LOOP)],
            "blocked": [{"key": "c:" + BLOCKED, "kind": "config", "entity_id": BLOCKED, "until": now + 20 * 3600, "n": 1,
                         "code": "source.hub.request_failed"}]}


def bridge_frame(now: float) -> dict:
    return {"type": "hub_bridge", "source": "hub", "t": now, "admin": True,
            "sent": {"get_states": 6, "config/device_registry/list": 6, "config/entity_registry/list": 6,
                     "config/area_registry/list": 6, "config_entries/get": 6, "subscribe_events": 3,
                     "automation/config": len(AUTOMATIONS), "script/config": len(SCRIPTS), "search/related": 8, "trace/list": 2,
                     "trace/get": 2},
            "refused": 0, "private_dropped": 23, "unknown_types": [],
            "budgets": {"config": {"per_min": hub_src.CONFIG_PER_MIN, "waits": 0},
                        "related": {"per_min": hub_src.RELATED_PER_MIN, "waits": 4},
                        "trace": {"per_min": hub_src.TRACE_PER_MIN, "waits": 0}},
            "blocked": 1, "trace_queue": 0, "items": len(AUTOMATIONS) + len(SCRIPTS), "trackers": 4}


def traces(entity_id: str, now: float) -> dict | None:
    known = {e: (cid, x) for e, _n, _m, cid, _r, x in AUTOMATIONS}
    known.update({e: (e.split(".", 1)[1], x) for e, _n, _m, x in SCRIPTS})
    if entity_id not in known or not known[entity_id][0]:
        return None
    rng = random.Random(entity_id)
    runs = []
    for i in range(5):
        start = now - (i * 1300 + rng.randint(30, 900))
        stopped = i == 3
        runs.append({"run_id": "%032x" % rng.getrandbits(128), "start": start, "finish": start + rng.uniform(0.05, 2.4),
                     "state": "stopped", "execution": "failed_conditions" if stopped else "finished",
                     "last_step": "condition/0" if stopped else "action/%d" % max(0, len(known[entity_id][1].get("action", [])) - 1),
                     "error": False})
    t0 = runs[0]["start"]
    steps = [{"path": "trigger/0", "t": t0, "ok": True}]
    if known[entity_id][1].get("condition"):
        steps.append({"path": "condition/0", "t": t0 + 0.01, "ok": True})
    for n, _e in enumerate(known[entity_id][1].get("action", [])):
        steps.append({"path": "action/%d" % n, "t": t0 + 0.05 + n * 0.2, "ok": True})
    return {"type": "hub_traces", "source": "hub", "t": now, "entity_id": entity_id, "runs": runs, "steps": steps}


def by_for(entity_id: str, rng: random.Random) -> str | None:
    """Who caused a demo state change: an automation switching what it controls, sometimes a person."""
    for eid, _n, _m, _c, _r, x in AUTOMATIONS:
        if entity_id in x.get("action", []) and rng.random() < 0.7:
            return eid
    return "user" if rng.random() < 0.1 else None
