"""Demo data of the hub bridge: automations, scripts, run counts, dependencies, a script loop, a blocked lookup,
traces and the bridge counters. Fictional names only; nothing is contacted."""
from __future__ import annotations

import math
import random
import time

from .sources import hub as hub_src

def _state(eid, to=None, for_=None, **kw):
    t = dict({"trigger": "state", "entity_id": eid}, **kw)
    if to is not None:
        t["to"] = to
    if for_:
        t["for"] = for_
    return t


def _call(svc, *eids, **data):
    step = {"action": svc}
    if eids:
        step["target"] = {"entity_id": list(eids)}
    if data:
        step["data"] = data
    return step


# Configurations as the hub returns them (fictional). The bridge keeps only their structure (sources/hub_flow.py);
# aliases, descriptions and service data below are there to prove that they are dropped.
CONFIGS = {
    "automation.hallway_light_on_motion": {
        "alias": "Hallway light on motion", "description": "dim at night",
        "triggers": [_state("binary_sensor.hallway_4", "on")],
        "conditions": [{"condition": "numeric_state", "entity_id": "sensor.hallway_1", "below": 20}],
        "actions": [_call("light.turn_on", "light.hallway_2", brightness_pct=40), {"delay": "0:02:00"},
                    _call("light.turn_off", "light.hallway_2")]},
    "automation.garden_irrigation": {
        "triggers": [{"trigger": "sun", "event": "sunrise", "offset": "-00:30:00"}],
        "conditions": [{"condition": "numeric_state", "entity_id": "sensor.garden_1", "below": 40}],
        "actions": [{"action": "script.irrigation_cycle"}]},
    "automation.kitchen_climate": {
        "triggers": [{"trigger": "numeric_state", "entity_id": "sensor.kitchen_1", "above": 24}],
        "actions": [_call("switch.turn_on", "switch.kitchen_3"),
                    {"wait_for_trigger": [{"trigger": "numeric_state", "entity_id": "sensor.kitchen_1", "below": 22}],
                     "timeout": "1:00:00"},
                    _call("switch.turn_off", "switch.kitchen_3")]},
    "automation.office_power_saver": {
        "triggers": [_state("binary_sensor.office_4", "off", "0:15:00")],
        "conditions": [{"condition": "time", "after": "18:00:00", "before": "07:00:00"}],
        "actions": [_call("switch.turn_off", "switch.office_3"), _call("light.turn_off", "light.office_2")]},
    "automation.window_open_heating_off": {
        "triggers": [_state(["binary_sensor.living_room_4", "binary_sensor.bedroom_4"], "on", {"minutes": 1})],
        "actions": [_call("switch.turn_off", "switch.living_room_3")]},
    "automation.living_room_scene_sync": {
        "triggers": [_state("light.living_room_2")],
        "actions": [_call("light.turn_on", "light.living_room_6"), _call("light.toggle", "light.living_room_2")]},
    "automation.morning_lights": {
        "triggers": [{"trigger": "time", "at": "06:30:00", "weekday": ["mon", "tue", "wed", "thu", "fri"]},
                     {"trigger": "time", "at": "input_datetime.weekend_wake_up", "weekday": ["sat", "sun"]}],
        "actions": [_call("light.turn_on", "light.kitchen_2", "light.hallway_2"),
                    {"choose": [{"conditions": [{"condition": "numeric_state", "entity_id": "sensor.living_room_1", "below": 19}],
                                 "sequence": [_call("switch.turn_on", "switch.living_room_3")]}],
                     "default": [{"action": "notify.family_phones", "data": {"message": "Good morning"}}]}]},
    "automation.evening_blinds": {
        "triggers": [{"trigger": "sun", "event": "sunset", "offset": "00:15:00"}],
        "actions": [_call("cover.close_cover", "cover.ip_blind")]},
    "automation.night_mode": {
        "triggers": [{"trigger": "time", "at": "23:00:00"}],
        "actions": [{"action": "scene.turn_on", "target": {"entity_id": "scene.night_dimmed"}},
                    _call("light.turn_off", "light.living_room_2", "light.living_room_6")]},
    "automation.desk_focus": {
        "triggers": [_state("binary_sensor.mesh_motion_office", "on")],
        "actions": [_call("switch.turn_on", "switch.office_3"), {"delay": {"minutes": 10}}]},
    "automation.old_doorbell": {
        "triggers": [_state("binary_sensor.front_door_bell", "on")],
        "actions": [_call("media_player.play_media", "media_player.wlan_speaker_1", media_content_id="chime")]},
    "automation.vacuum_on_weekdays": {
        "triggers": [{"trigger": "time", "at": "10:00:00", "weekday": ["mon", "wed", "fri"]}],
        "conditions": [{"condition": "state", "entity_id": "lock.subghz_lock", "state": "locked"}],
        "actions": [_call("vacuum.start", "vacuum.wlan_vacuum_1"), {"delay": "1:30:00"},
                    _call("vacuum.return_to_base", "vacuum.wlan_vacuum_1")]},
    "automation.fridge_too_warm": {
        "triggers": [{"trigger": "numeric_state", "entity_id": "sensor.ble_fridge", "above": 8, "for": "0:10:00"}],
        "actions": [{"action": "notify.notify", "data": {"message": "Fridge {{ states('sensor.ble_fridge') }}"}}]},
    "automation.battery_check": {
        "triggers": [{"trigger": "time_pattern", "minutes": "/30"}],
        "conditions": [{"condition": "numeric_state", "entity_id": "sensor.mesh_remote_living_battery", "below": 10}],
        "actions": [{"action": "persistent_notification.create", "data": {"message": "Battery low"}}]},
    "automation.garden_light_on_motion": {
        "mode": "single",
        "triggers": [_state("binary_sensor.garden_4", "on")],
        "conditions": [{"condition": "sun", "after": "sunset", "before": "sunrise", "before_offset": "-00:20:00"}],
        "actions": [_call("light.turn_on", "light.garden_2"), {"delay": "0:05:00"}, _call("light.turn_off", "light.garden_2")]},
    "automation.holiday_lights": {
        "triggers": [{"trigger": "sun", "event": "sunset", "id": "dusk"}, {"trigger": "time", "at": "23:30:00", "id": "late"}],
        "actions": [{"if": [{"condition": "trigger", "id": "dusk"}], "then": [_call("switch.turn_on", "switch.garden_3")],
                     "else": [_call("switch.turn_off", "switch.garden_3")]}]},
    "script.irrigation_cycle": {
        "sequence": [_call("switch.turn_on", "switch.garden_3"), {"delay": "0:20:00"}, _call("switch.turn_off", "switch.garden_3"),
                     {"action": "script.irrigation_check"}]},
    "script.irrigation_check": {
        "sequence": [{"condition": "numeric_state", "entity_id": "sensor.garden_1", "below": 30}, {"action": "script.irrigation_cycle"}]},
    "script.good_night": {
        "sequence": [_call("light.turn_off", "light.living_room_2", "light.kitchen_2"), _call("switch.turn_off", "switch.office_3"),
                     {"repeat": {"count": 2, "sequence": [_call("light.toggle", "light.hallway_2"), {"delay": 1}]}}]},
}


class _Everything:
    def __contains__(self, _x):
        return True


def _refs(eid):
    from .sources import hub_deps
    return {k: v for k, v in hub_deps.refs_from_config(CONFIGS.get(eid) or {}, _Everything()).items() if k != "text"}


# entity id, name, mode, config id (None: no traces), runs per hour, what it reads / switches, state, last run (days ago)
_AUTO_DEFS = [
    ("automation.hallway_light_on_motion", "Hallway light on motion", "restart", "1001", 14, "on", None),
    ("automation.garden_irrigation", "Garden irrigation at dawn", "single", "1002", 0.05, "on", None),
    ("automation.kitchen_climate", "Kitchen climate", "queued", "1003", 3, "on", None),
    ("automation.office_power_saver", "Office power saver", "single", "1004", 0.5, "on", None),
    ("automation.window_open_heating_off", "Window open: heating off", "single", "1005", 1, "on", None),
    ("automation.garage_door_reminder", "Garage door reminder", "single", None, 0.1, "on", None),
    ("automation.living_room_scene_sync", "Living room scene sync", "parallel", "1007", 96, "on", None),
    ("automation.morning_lights", "Morning lights", "single", "1008", 0, "on", 0.4),
    ("automation.evening_blinds", "Close the blinds after sunset", "single", "1009", 0, "on", 0.8),
    ("automation.night_mode", "Night mode", "single", "1010", 0, "on", 0.6),
    ("automation.desk_focus", "Desk focus", "restart", "1011", 2, "on", None),
    ("automation.old_doorbell", "Old doorbell chime", "single", "1012", 0, "on", 64),
    ("automation.vacuum_on_weekdays", "Vacuum on weekdays", "single", "1013", 0, "on", 2.2),
    ("automation.fridge_too_warm", "Fridge too warm", "single", "1014", 0, "on", 9),
    ("automation.battery_check", "Battery check", "single", "1015", 2, "on", None),
    ("automation.garden_light_on_motion", "Garden light on motion", "single", "1016", 1, "on", None),
    ("automation.holiday_lights", "Holiday lights", "single", "1017", 0, "off", 300),
]
AUTOMATIONS = [(e, n, m, c, r, _refs(e)) for e, n, m, c, r, _s, _d in _AUTO_DEFS]
STATE = {e: s for e, _n, _m, _c, _r, s, _d in _AUTO_DEFS}
LAST_DAYS = {e: d for e, _n, _m, _c, _r, _s, d in _AUTO_DEFS if d is not None}
SCRIPTS = [
    ("script.irrigation_cycle", "Irrigation cycle", "single", _refs("script.irrigation_cycle")),
    ("script.irrigation_check", "Irrigation check", "single", _refs("script.irrigation_check")),
    ("script.good_night", "Good night", "single", _refs("script.good_night")),
]
LOOP = ["script.irrigation_check", "script.irrigation_cycle"]
BLOCKED = "automation.garage_door_reminder"
# integration entries of the sample hub (domain, state, count): one that cannot connect, one that retries
ENTRIES = [("sample_hub", "loaded", 1), ("sample_wlan", "loaded", 4), ("sample_mesh", "loaded", 1), ("sample_subghz", "loaded", 1),
           ("sample_thread", "loaded", 1), ("sample_ipstd", "loaded", 1), ("sample_ble", "loaded", 2), ("sample_weather", "setup_retry", 1),
           ("sample_cloud_camera", "setup_error", 1), ("sample_backup", "not_loaded", 1)]
# who switches what in the sample: (entity, cause, state, seconds after the start of the hour); the power saver and the
# desk focus switch the office plug in opposite directions a few seconds apart (a conflict)
CHANGE_PATTERN = [
    ("switch.office_3", "automation.desk_focus", "on", 600), ("switch.office_3", "automation.office_power_saver", "off", 640),
    ("light.hallway_2", "automation.hallway_light_on_motion", "on", 900), ("light.hallway_2", "automation.hallway_light_on_motion", "off", 1020),
    ("light.living_room_2", "user", "on", 1500), ("light.living_room_6", "automation.living_room_scene_sync", "on", 1501),
    ("switch.kitchen_3", "automation.kitchen_climate", "on", 2100), ("switch.kitchen_3", None, "off", 2700),
    ("binary_sensor.hallway_4", None, "on", 899), ("binary_sensor.office_4", None, "off", 3000),
    ("switch.garden_3", "script.irrigation_cycle", "on", 3300),
]


def items() -> tuple[list, list]:
    autos = [{"entity_id": e, "id": cid, "mode": mode, "last": None, "state": STATE[e]} for e, _n, mode, cid, _r, _x in AUTOMATIONS]
    scripts = [{"entity_id": e, "id": e.split(".", 1)[1], "mode": mode, "last": None, "state": "off"} for e, _n, mode, _x in SCRIPTS]
    return autos, scripts


RENAME = {"binary_sensor.hallway_4": ("Hallway motion", "motion"), "binary_sensor.office_4": ("Office presence", "occupancy"),
          "binary_sensor.garden_4": ("Garden motion", "motion")}


def sun_times(now: float) -> dict:
    """Sample sunrise 07:12 and sunset 16:38 local time (the next ones after ``now``)."""
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    rise, sset = midnight + 7 * 3600 + 12 * 60, midnight + 16 * 3600 + 38 * 60
    return {"rise": rise + (86400 if rise < now else 0), "set": sset + (86400 if sset < now else 0)}


def extend_snapshot(snap: dict, now: float) -> dict:
    """Automations/scripts as entities and items, hardware addresses of three devices, tracker count."""
    autos, scripts = items()
    for i, (eid, name, *_rest) in enumerate(AUTOMATIONS):
        autos[i]["last"] = now - LAST_DAYS[eid] * 86400 if eid in LAST_DAYS else now - 60 * (i * 7 + 3)
        snap["entities"].append({"entity_id": eid, "device_id": None, "name": name, "state": STATE[eid], "unit": None,
                                 "domain": "automation", "area": None})
    for eid, name, *_rest in SCRIPTS:
        snap["entities"].append({"entity_id": eid, "device_id": None, "name": name, "state": "off", "unit": None,
                                 "domain": "script", "area": None})
    devices, entities, trackers = radio_snapshot(now)
    snap["devices"].extend(devices)
    snap["entities"].extend(entities)
    snap["automations"], snap["scripts"] = autos, scripts
    snap["trackers"] = trackers
    for e in snap["entities"]:                    # the window contacts of the rooms, two of them motion sensors
        if e.get("domain") == "binary_sensor" and not e.get("dc"):
            e["dc"] = "window"
        if e["entity_id"] in RENAME:
            e["name"], e["dc"] = RENAME[e["entity_id"]]
    snap["entities"].append({"entity_id": "input_datetime.weekend_wake_up", "device_id": None, "name": "Weekend wake-up",
                             "state": "08:15:00", "unit": None, "domain": "input_datetime", "area": None, "dc": None, "cat": None,
                             "platform": "input_datetime", "ls": False})
    snap["entities"].append({"entity_id": "sun.sun", "device_id": None, "name": "Sun", "state": "below_horizon", "unit": None,
                             "domain": "sun", "area": None, "dc": None, "cat": None, "platform": "sun", "ls": False})
    snap["entries"] = [{"domain": d, "state": st, "n": n} for d, st, n in ENTRIES]
    snap["sun"] = sun_times(now)
    snap["admin"] = True
    from . import demo_energy
    return demo_energy.extend(snap, now)


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


def flow(eid: str) -> dict | None:
    from .sources import hub_flow
    return hub_flow.flow_from_config(CONFIGS[eid]) if eid in CONFIGS else None


def deps_frame(now: float) -> dict:
    autos = {e: dict({r: list(x.get(r, [])) for r in ("trigger", "condition", "action")}, text=[], related=list(x.get("action", [])),
                     flow=flow(e))
             for e, _n, _m, _c, _r, x in AUTOMATIONS if e != BLOCKED}
    scripts = {e: {"trigger": [], "condition": [], "action": list(x["action"]), "text": [], "related": [], "flow": flow(e)}
               for e, _n, _m, x in SCRIPTS}
    return {"type": "hub_deps", "source": "hub", "t": now - 240, "automations": autos, "scripts": scripts, "cycles": [list(LOOP)],
            "blocked": [{"key": "c:" + BLOCKED, "kind": "config", "entity_id": BLOCKED, "until": now + 20 * 3600, "n": 1,
                         "code": "source.hub.request_failed"}]}


def change_frames(now: float, hours: int = 24) -> list:
    """State changes with their cause over the last hours (who switches what, the timeline), on a fixed pattern."""
    out = []
    for h in range(hours, 0, -1):
        base = (int(now // 3600) - h) * 3600
        night = 1 <= time.localtime(base).tm_hour <= 5
        for eid, by, state, off in CHANGE_PATTERN:
            if night and by not in ("automation.office_power_saver", "automation.desk_focus"):
                continue
            ev = {"type": "hub_state", "source": "hub", "entity_id": eid, "state": state, "t": base + off}
            if by:
                ev["by"] = by
            out.append(ev)
    return out


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


# who switches the sample entities in the live demo: one owner each, so the only conflict is the curated one (desk focus
# switches the office plug on, the power saver off)
OWNERS = {"light.hallway_2": {"on": "automation.hallway_light_on_motion", "off": "automation.hallway_light_on_motion"},
          "switch.kitchen_3": {"on": "automation.kitchen_climate", "off": None},
          "switch.office_3": {"on": "automation.desk_focus", "off": "automation.office_power_saver"},
          "light.living_room_2": {"on": "user", "off": "user"}}


def by_for(entity_id: str, rng: random.Random, state: str | None = None) -> str | None:
    """Who caused a demo state change (see ``OWNERS``); now and then the device itself."""
    owner = OWNERS.get(entity_id) or {}
    return owner.get(state) if rng.random() < 0.85 else None
