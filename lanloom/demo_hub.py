"""Demo data of the hub bridge: automations, scripts, run counts, dependencies, a script loop, a blocked lookup,
traces and the bridge counters. Fictional names only; nothing is contacted."""
from __future__ import annotations

import math
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
    devices, entities, trackers = radio_snapshot(now)
    snap["devices"].extend(devices)
    snap["entities"].extend(entities)
    snap["automations"], snap["scripts"] = autos, scripts
    snap["trackers"] = trackers
    snap["admin"] = True
    return snap


# ---------------------------------------------------------------------- merged devices and radio networks (H2)
def _host_mac(n: int) -> str:
    from . import demo
    return demo._hmac(n)


# hub devices that are also network devices: (id, name, area, demo host, how the demo shows the match, entities)
# how: "mac" (hardware address in the registry), "tracker" (configuration page + tracker), "ip" (configuration page),
# "name" (same name only: a suggestion)
NETWORK_SIDE = [
    ("wlan-washer", "Washer plug", "garage", 60, "mac", [("switch", "Washer plug", "on", None, None), ("sensor", "Washer power", "412.5", "W", "power")]),
    ("wlan-thermostat", "Hall thermostat", "hallway", 61, "mac", [("sensor", "Hall temperature", "21.4", "\u00b0C", "temperature"),
                                                               ("sensor", "Hall humidity", "46", "%", "humidity")]),
    ("wlan-meter", "Energy meter", "garage", 62, "mac", [("sensor", "Grid power", "1180", "W", "power"), ("sensor", "Energy today", "7.42", "kWh", "energy")]),
    ("wlan-strip", "Desk light strip", "office", 63, "mac", [("light", "Desk light strip", "on", None, None)]),
    ("wlan-tv", "Living room TV", "living_room", 30, "tracker", [("media_player", "Living room TV", "playing", None, None)]),
    ("wlan-vacuum", "Robot vacuum", "hallway", 65, "ip", [("vacuum", "Robot vacuum", "docked", None, None),
                                                          ("sensor", "Vacuum battery", "88", "%", "battery")]),
    ("wlan-speaker", "Kitchen speaker", "kitchen", 31, "name", [("media_player", "Kitchen speaker", "idle", None, None)]),
]
# radio networks: (key, kind, coordinator (id, name, model, area, role) or None)
RADIO_NETS = [
    ("mesh", "mesh154", ("radio-stick", "Radio stick", "USB coordinator", "office", "coordinator")),
    ("subghz", "subghz", ("subghz-controller", "Sub-GHz controller", "USB controller", "office", "coordinator")),
    ("thread", "thread", None),
    ("ipstd", "ipstd", None),
    ("ble", "ble", None),
]
# radio devices: (id, name, network key, area, battery % or None, last seen hours ago or None, role, main entity, silent)
RADIO_DEVICES = [
    ("mesh-motion-hall", "Hallway motion sensor", "mesh", "hallway", 74, 0.05, "device", ("binary_sensor", "motion", "off"), False),
    ("mesh-door-kitchen", "Kitchen door contact", "mesh", "kitchen", 91, 0.2, "device", ("binary_sensor", "door", "off"), False),
    ("mesh-window-bed", "Bedroom window contact", "mesh", "bedroom", 66, 0.4, "device", ("binary_sensor", "window", "off"), False),
    ("mesh-temp-living", "Living room climate", "mesh", "living_room", 58, 0.1, "device", ("sensor", "temperature", "21.8"), False),
    ("mesh-temp-office", "Office climate", "mesh", "office", 43, 0.3, "device", ("sensor", "temperature", "22.6"), False),
    ("mesh-soil-garden", "Garden soil moisture", "mesh", "garden", 33, 1.2, "device", ("sensor", "moisture", "38"), False),
    ("mesh-door-garage", "Garage door contact", "mesh", "garage", 81, 0.6, "device", ("binary_sensor", "door", "off"), False),
    ("mesh-humid-bath", "Bathroom humidity", "mesh", "hallway", 52, 0.2, "device", ("sensor", "humidity", "61"), False),
    ("mesh-switch-hall", "Hallway light switch", "mesh", "hallway", None, 0.01, "device", ("light", None, "on"), False),
    ("mesh-plug-kitchen", "Kitchen counter plug", "mesh", "kitchen", None, 0.02, "device", ("switch", None, "off"), False),
    ("mesh-valve-bed", "Bedroom radiator valve", "mesh", "bedroom", 18, 0.5, "device", ("sensor", "temperature", "19.5"), False),
    ("mesh-remote-living", "Living room remote", "mesh", "living_room", 7, 2.5, "device", ("sensor", "action", "idle"), False),
    ("mesh-motion-office", "Office motion sensor", "mesh", "office", 69, 0.1, "device", ("binary_sensor", "motion", "on"), False),
    ("mesh-gate-garden", "Garden gate contact", "mesh", "garden", 12, 30.0, "device", ("binary_sensor", "door", "off"), True),
    ("subghz-lock", "Front door lock", "subghz", "hallway", 64, None, "device", ("lock", None, "locked"), False),
    ("subghz-siren", "Hallway siren", "subghz", "hallway", None, None, "device", ("switch", None, "off"), False),
    ("thread-router", "Border router", "thread", "living_room", None, None, "border_router", ("sensor", "status", "running"), False),
    ("ip-desk-lamp", "Desk lamp", "ipstd", "office", None, None, "device", ("light", None, "off"), False),
    ("ip-hall-plug", "Hall smart plug", "ipstd", "hallway", None, None, "device", ("switch", None, "on"), False),
    ("ip-blind", "Bedroom blind", "ipstd", "bedroom", 46, None, "device", ("cover", None, "open"), True),
    ("ble-proxy-garden", "Garden radio proxy", "ble", "garden", None, None, "receiver", ("sensor", "signal", "-61"), False),
    ("ble-adapter", "Built-in BLE adapter", "ble", "office", None, None, "receiver", None, False),
    ("ble-fridge", "Fridge thermometer", "ble", "kitchen", 81, None, "device", ("sensor", "temperature", "4.1"), False),
    ("ble-freezer", "Freezer thermometer", "ble", "kitchen", 55, None, "device", ("sensor", "temperature", "-18.2"), False),
    ("ble-plant", "Plant sensor", "ble", "living_room", 22, None, "device", ("sensor", "moisture", "29"), False),
]
UNITS = {"temperature": "\u00b0C", "humidity": "%", "moisture": "%", "signal": "dBm"}


def _iso(t: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec="seconds")


def radio_snapshot(now: float) -> tuple:
    """Demo hub devices on the network side (matched with demo hosts) and in five radio networks."""
    devices, entities = [], []

    def ent(eid, did, name, state, unit=None, dc=None, area=None, cat=None, ls=False, platform="sample_hub"):
        entities.append({"entity_id": eid, "device_id": did, "name": name, "state": state, "unit": unit,
                         "domain": eid.split(".", 1)[0], "area": area, "dc": dc, "cat": cat, "platform": platform, "ls": ls})
    for did, name, area, host, how, ents in NETWORK_SIDE:
        d = {"id": did, "name": name, "model": None, "manufacturer": None, "area": area, "via": None, "service": False,
             "macs": [_host_mac(host)] if how == "mac" else [], "sw": "1.%d.0" % (host % 7), "domains": ["sample_hub"],
             "radio": None, "role": None, "ip_hint": "192.168.50.%d" % host if how in ("ip", "tracker") else None, "host_hint": None}
        devices.append(d)
        for n, (dom, ename, state, unit, dc) in enumerate(ents):
            ent("%s.%s_%d" % (dom, did.replace("-", "_"), n + 1), did, ename, state, unit, dc, area, platform="sample_wlan")
    coords = {}
    for key, kind, coord in RADIO_NETS:
        if coord:
            cid, cname, model, area, role = coord
            coords[key] = cid
            devices.append({"id": cid, "name": cname, "model": model, "manufacturer": None, "area": area, "via": None,
                            "service": False, "macs": [], "sw": "7.4.2", "domains": ["sample_" + key], "radio": kind, "role": role,
                            "ip_hint": None, "host_hint": None})
            ent("sensor.%s_status" % cid.replace("-", "_"), cid, cname + " status", "online", area=area, cat="diagnostic",
                platform="sample_" + key)
    kinds = {k: kind for k, kind, _c in RADIO_NETS}
    for did, name, key, area, battery, seen_h, role, main, silent in RADIO_DEVICES:
        macs = [_host_mac(64)] if did == "ble-proxy-garden" else []
        devices.append({"id": did, "name": name, "model": None, "manufacturer": None, "area": area, "via": coords.get(key),
                        "service": False, "macs": macs, "sw": None, "domains": ["sample_" + key], "radio": kinds[key], "role": role,
                        "ip_hint": None, "host_hint": None})
        base = did.replace("-", "_")
        plat = "sample_" + key
        if main:
            dom, dc, state = main
            unit = UNITS.get(dc) if dom == "sensor" else None
            ent("%s.%s" % (dom, base), did, name, "unavailable" if silent else state, unit, dc if dom in ("sensor", "binary_sensor") else None,
                area, platform=plat)
        if battery is not None:
            ent("sensor.%s_battery" % base, did, name + " battery", "unavailable" if silent else str(battery), "%", "battery", area,
                cat="diagnostic", platform=plat)
        if seen_h is not None:
            ent("sensor.%s_last_seen" % base, did, name + " last seen", _iso(now - seen_h * 3600), None, "timestamp", area,
                cat="diagnostic", ls=True, platform=plat)
    trackers = [{"mac": _host_mac(30), "ip": "192.168.50.30"}] + [{"mac": "02:00:5e:10:00:%02x" % (0x40 + n), "ip": None} for n in range(3)]
    return devices, entities, trackers


def radio_tick(now: float, rng: random.Random) -> list:
    """Live state changes of the demo radio devices: a fresh "last seen" and a new value every few seconds."""
    out = []
    for did, name, key, _area, _battery, seen_h, _role, main, silent in RADIO_DEVICES:
        if silent or rng.random() > 0.08:
            continue
        base = did.replace("-", "_")
        if seen_h is not None and seen_h < 1.5:
            out.append({"type": "hub_state", "source": "hub", "entity_id": "sensor.%s_last_seen" % base, "state": _iso(now), "t": now})
        if main and main[0] == "sensor" and main[1] in UNITS:
            try:
                v = float(main[2]) + rng.uniform(-0.6, 0.6)
            except ValueError:
                continue
            out.append({"type": "hub_state", "source": "hub", "entity_id": "sensor.%s" % base, "state": "%.1f" % v, "t": now})
        elif main and main[0] == "binary_sensor":
            out.append({"type": "hub_state", "source": "hub", "entity_id": "binary_sensor.%s" % base,
                        "state": rng.choice(["on", "off"]), "t": now})
    return out


def history(entity: dict, rng_name: str, numeric: bool, now: float) -> dict:
    """Recorded course of a demo entity: hourly statistics for numbers, state changes otherwise (no hub contacted)."""
    from .hubsensors import SPAN_S, num
    span = SPAN_S[rng_name]
    r = random.Random(entity.get("entity_id"))
    out = {"stats": [], "states": []}
    base = num(entity.get("state"))
    if numeric:
        base = 20.0 if base is None else base
        amp = max(0.5, abs(base) * 0.08)
        if rng_name == "24h":
            t = now - span
            while t <= now:
                day = math.sin((t % 86400) / 86400 * 2 * math.pi - 1.6)
                out["states"].append([t, "%.2f" % (base + amp * day + r.uniform(-amp, amp) * 0.25)])
                t += 300
        else:
            t = now - span
            while t <= now:
                day = math.sin((t % 86400) / 86400 * 2 * math.pi - 1.6)
                mean = base + amp * day + amp * 0.4 * math.sin(t / 86400 / 3.1)
                spread = amp * (0.15 + 0.2 * r.random())
                out["stats"].append([t, mean - spread, mean, mean + spread])
                t += 3600
        return out
    states = {"binary_sensor": ["off", "on"], "light": ["off", "on"], "switch": ["off", "on"], "lock": ["locked", "unlocked"],
              "cover": ["closed", "open"], "media_player": ["off", "idle", "playing"], "vacuum": ["docked", "cleaning", "returning"]}
    choices = states.get(entity.get("domain"), [entity.get("state") or "on"])
    t = now - span
    cur = choices[0]
    while t <= now:
        out["states"].append([t, cur])
        t += r.uniform(0.3, 6.0) * 3600 * (span / 86400) ** 0.5
        cur = r.choice([c for c in choices if c != cur] or choices)
    return out


def triggers_frame(now: float, rng: random.Random, dt: float = 10.0) -> dict | None:
    runs = {}
    for eid, _n, _m, _c, per_hour, _x in AUTOMATIONS:
        expect = per_hour * dt / 3600.0
        n = int(expect) + (1 if rng.random() < expect - int(expect) else 0)
        if n:
            runs[eid] = {"n": n, "last": now - rng.random() * dt}
    return {"type": "hub_triggers", "source": "hub", "t": now, "runs": runs} if runs else None


def hub_src_busy() -> int:
    from .hubbridge import BUSY_PER_HOUR
    return BUSY_PER_HOUR


def history_frames(now: float, rng: random.Random) -> list:
    """Run counts of the last 24 hours (so the bridge card and the nightly report have something to show at once)."""
    frames = []
    for h in range(24, 0, -1):
        t = now - h * 3600 + 1800
        runs = {}
        for eid, _n, _m, _c, per_hour, _x in AUTOMATIONS:
            # quieter at night - except the runaway automation, which never sleeps (the card shows it at any hour)
            night = 0.3 if 1 <= time.localtime(t).tm_hour <= 6 and per_hour < hub_src_busy() else 1.0
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
