"""The Automations tab (core side, read only): list, findings, "In plain words", the flow, the dependency board, who
switches what, the timeline, the day plan, devices per room and the hub's health.

Everything is built from what the bridge already keeps in memory (``core.hub[integration]``): the snapshot (entities,
devices, areas, integration states), the live states, the flows of automations and scripts (admin mode, see
``sources/hub_flow.py``), the run counts and the state changes with their cause (an automation or script, ``user`` for
a person - never who - or none: the device or the hub itself). Nothing is sent to the hub here and nothing is changed:
the tab explains, it never heals.

"In plain words" (``explain``) is rule based, without any AI: every step of a flow becomes one sentence spec
``{"k": i18n key, "v": vars, "tail": [specs], "sub": [specs], "then": [specs]}`` that the browser renders in the user's
language. Names come from the hub (entity names); an entity the hub does not know is written as its humanised id.

Findings (``FINDINGS``) per automation, from its own rules:

* ``dead``         names an entity the hub does not have (renamed or removed)
* ``unavailable``  names an entity that is ``unavailable`` right now
* ``runaway``      ran ``hubbridge.BUSY_PER_HOUR`` times or more within one clock hour
* ``never``        switched on, but has not run for ``NEVER_DAYS`` days (or never)
* ``single_wait``  mode ``single`` with a wait or delay: starts that come while it waits are dropped
* ``self_trigger`` switches an entity that also starts it
* ``conflict``     another automation set the same entity to the opposite state within ``CONFLICT_S`` seconds
* ``mode``         runs the hub dropped, stopped or queued out because of the automation's mode (from the stored runs,
                   see ``hubchain``): ``single`` dropped a start, ``restart`` stopped runs three times within an hour,
                   ``queued``/``parallel`` hit the maximum number of runs
* ``loop``         a script loop (from the bridge)
* ``blocked``      the bridge pauses its lookups (the hub answered with an error)
"""
from __future__ import annotations

import time

from . import hub_privacy as privacy
from . import hubbridge, hubchain
from .sources import hub_flow

NEVER_DAYS = 30
CONFLICT_S = 60.0                 # two automations setting one entity to opposite states within this window: a conflict
FINDINGS = ("dead", "unavailable", "runaway", "conflict", "mode", "self_trigger", "loop", "single_wait", "never", "blocked")
LEVEL = {"dead": "warn", "unavailable": "warn", "runaway": "warn", "conflict": "warn", "mode": "warn", "self_trigger": "info",
         "loop": "warn", "single_wait": "info", "never": "info", "blocked": "info"}
MAX_NAMES = 6
MAX_WHO = 200
MAX_TIMELINE = 400
MAX_RAN = 60
MAX_ROOM_ENTITIES = 8
MAX_UNAVAILABLE = 12
# state changes of these domains appear on the timeline also when nothing is known about their cause (numbers of
# plain sensors would flood it)
TIMELINE_DOMAINS = frozenset(("light", "switch", "cover", "lock", "climate", "fan", "media_player", "input_boolean",
                              "input_select", "input_number", "binary_sensor", "alarm_control_panel", "vacuum", "scene",
                              "siren", "valve", "water_heater", "humidifier", "automation", "script", "button"))
ROOM_SKIP_DOMAINS = frozenset(("automation", "script", "update", "button", "event", "scene"))
PROBLEM_ENTRY_STATES = ("setup_error", "setup_retry", "migration_error", "failed_unload")
OPPOSITE = {("on", "off"), ("off", "on"), ("open", "closed"), ("closed", "open"), ("locked", "unlocked"),
            ("unlocked", "locked"), ("playing", "paused"), ("paused", "playing"), ("heat", "off"), ("off", "heat")}
WAIT_MIN_S = 30                   # a delay shorter than this rarely drops a start


def humanise(entity_id: str) -> str:
    obj = str(entity_id).split(".", 1)[-1]
    return obj.replace("_", " ").strip().capitalize() or str(entity_id)


# ---------------------------------------------------------------------- context: one hub connection at one moment
class Ctx:
    def __init__(self, entry: dict, now: float | None = None):
        self.now = time.time() if now is None else now
        self.entry = entry or {}
        snap = self.entry.get("snapshot") or {}
        self.snap = snap
        self.live = self.entry.get("states") or {}
        self.ents = {e["entity_id"]: e for e in snap.get("entities") or [] if isinstance(e, dict) and e.get("entity_id")}
        self.devs = {d["id"]: d for d in snap.get("devices") or [] if isinstance(d, dict) and d.get("id")}
        self.areas = {a["id"]: a.get("name") or a["id"] for a in snap.get("areas") or [] if isinstance(a, dict) and a.get("id")}
        deps = self.entry.get("deps") or {}
        self.deps = deps
        self.refs = dict(deps.get("automations") or {}, **(deps.get("scripts") or {}))
        self.flows = {e: r["flow"] for e, r in self.refs.items() if isinstance(r, dict) and isinstance(r.get("flow"), dict)}
        self.items = {i["entity_id"]: i for i in (snap.get("automations") or []) + (snap.get("scripts") or [])
                      if isinstance(i, dict) and i.get("entity_id")}
        self.admin = bool(snap.get("admin"))

    def name(self, eid) -> str:
        e = self.ents.get(eid)
        return (e or {}).get("name") or humanise(eid)

    def state(self, eid):
        got = self.live.get(eid)
        if got:
            return got[0]
        return (self.ents.get(eid) or {}).get("state")

    def area_of(self, eid):
        e = self.ents.get(eid) or {}
        a = e.get("area") or (self.devs.get(e.get("device_id")) or {}).get("area")
        return a if a in self.areas else None

    def known(self, eid) -> bool:
        return eid in self.ents or eid in self.items

    def ent_info(self, eid) -> dict:
        e = self.ents.get(eid) or {}
        st = self.state(eid)
        return {"id": eid, "name": self.name(eid), "domain": privacy.domain_of(eid), "state": st, "unit": e.get("unit"),
                "area": self.areas.get(self.area_of(eid)), "missing": not self.known(eid),
                "unavailable": st == "unavailable", "item": privacy.domain_of(eid) in ("automation", "script")}


# ---------------------------------------------------------------------- "In plain words": rule-based sentence specs
P = "auto.ex."


def _spec(key: str, many: bool = False, tail=None, sub=None, then=None, **v) -> dict:
    out = {"k": P + key + (".many" if many else ""), "v": v}
    if tail:
        out["tail"] = [t for t in tail if t]
    if sub is not None:
        out["sub"] = sub
    if then is not None:
        out["then"] = then
    return out


def _dur_tail(key: str, secs):
    return _spec(key, dur=abs(int(secs))) if secs else None


def _off_tail(off):
    if not off:
        return None
    return _spec("tail.before" if off < 0 else "tail.after", dur=abs(int(off)))


DOOR = ("door", "window", "opening", "garage_door")
MOTION = ("motion", "occupancy", "presence", "moving")


def _klass(ctx: Ctx, ids: list) -> str:
    """What kind of thing the entities are, for the verb: door, motion, switch, cover, lock, media, sun or plain."""
    if not ids:
        return "plain"
    dom = privacy.domain_of(ids[0])
    if dom == "sun":
        return "sun"
    if dom == "binary_sensor":
        dc = (ctx.ents.get(ids[0]) or {}).get("dc")
        if dc in DOOR:
            return "door"
        if dc in MOTION:
            return "motion"
        return "binary"
    if dom in ("light", "switch", "fan", "input_boolean", "automation", "script", "siren", "humidifier"):
        return "switch"
    if dom in ("cover", "valve"):
        return "cover"
    if dom == "lock":
        return "lock"
    if dom == "media_player":
        return "media"
    return "plain"


STATE_VERBS = {  # (class, state) -> key part for triggers ("t.") and conditions ("c.")
    ("switch", "on"): "on", ("switch", "off"): "off", ("door", "on"): "open", ("door", "off"): "closed",
    ("motion", "on"): "motion", ("motion", "off"): "still", ("binary", "on"): "on", ("binary", "off"): "off",
    ("cover", "open"): "open", ("cover", "closed"): "closed", ("cover", "opening"): "open", ("cover", "closing"): "closed",
    ("lock", "locked"): "locked", ("lock", "unlocked"): "unlocked", ("media", "playing"): "playing",
    ("media", "paused"): "paused", ("media", "idle"): "paused", ("sun", "above_horizon"): "sunrise",
    ("sun", "below_horizon"): "sunset",
}


def _numv(ctx: Ctx, v, ids):
    if isinstance(v, str):
        return {"name": ctx.name(v)}
    unit = (ctx.ents.get(ids[0]) or {}).get("unit") if ids else None
    return {"num": v, "unit": unit}


def _names(ctx: Ctx, ids) -> list:
    return [ctx.name(e) for e in ids or [] if isinstance(e, str)]


def trigger_spec(ctx: Ctx, n: dict) -> dict:
    k = n.get("k")
    ids = n.get("e") or []
    who = _names(ctx, ids)
    many = False                     # "A or B opens": one of them is enough, the verb stays singular
    tail = [_dur_tail("tail.for", n.get("for"))]
    if k == "state":
        to = (n.get("to") or [None])[0] if len(n.get("to") or []) == 1 else None
        klass = _klass(ctx, ids)
        if to == "unavailable":
            return _spec("t.unavailable", many, tail, who=who)
        if to is not None:
            verb = STATE_VERBS.get((klass, to))
            if verb == "sunrise" or verb == "sunset":
                return _spec("t." + verb, tail=tail)
            if verb:
                return _spec("t." + verb, many, tail, who=who)
            return _spec("t.state", many, tail, who=who, state=to)
        if n.get("to"):
            return _spec("t.states", many, tail, who=who, states=n["to"])
        if n.get("from"):
            return _spec("t.leaves", many, tail, who=who, state=n["from"][0])
        return _spec("t.change", many, tail + ([_spec("tail.attr")] if n.get("attr") else []), who=who)
    if k == "num":
        lo, hi = n.get("above"), n.get("below")
        if lo is not None and hi is not None:
            return _spec("t.between", many, tail, who=who, lo=_numv(ctx, lo, ids), hi=_numv(ctx, hi, ids))
        if lo is not None:
            return _spec("t.above", many, tail, who=who, val=_numv(ctx, lo, ids))
        if hi is not None:
            return _spec("t.below", many, tail, who=who, val=_numv(ctx, hi, ids))
        return _spec("t.change", many, tail, who=who)
    if k == "time":
        times = [a for a in n.get("at") or [] if ":" in a and "." not in a]
        helpers = [a for a in n.get("at") or [] if "." in a]
        wd = [_spec("tail.wd", wd=n["wd"])] if n.get("wd") else []
        if times:
            return _spec("t.time", tail=wd, at=times)
        if helpers:
            return _spec("t.time_helper", tail=wd, who=_names(ctx, helpers))
        return _spec("t.time_tpl", tail=wd)
    if k == "pattern":
        ev = n.get("every") or {}
        for unit, key in (("s", "t.every_s"), ("m", "t.every_m"), ("h", "t.every_h")):
            v = ev.get(unit, "")
            if v.startswith("/") and v[1:].isdigit():
                return _spec(key, n=int(v[1:]))
        if ev.get("m", "").isdigit() and ev.get("h", "*") == "*":
            return _spec("t.hourly", minute=int(ev["m"]))
        return _spec("t.pattern")
    if k == "sun":
        return _spec("t." + (n.get("ev") or "sunset"), tail=[_off_tail(n.get("off"))])
    if k == "start":
        return _spec("t.shutdown" if n.get("ev") == "shutdown" else "t.start")
    if k in ("event", "mqtt", "webhook", "device", "zone", "calendar", "tag", "tpl"):
        return _spec("t." + k, tail=tail)
    return _spec("t.other")


def condition_spec(ctx: Ctx, n: dict) -> dict:
    k = n.get("k")
    ids = n.get("e") or []
    who = _names(ctx, ids)
    many = len(ids) > 1
    tail = [_dur_tail("tail.for", n.get("for"))]
    if k == "state":
        to = n.get("to") or []
        if len(to) == 1:
            verb = STATE_VERBS.get((_klass(ctx, ids), to[0]))
            if verb in ("sunrise", "sunset"):
                return _spec("c.sun_up" if verb == "sunrise" else "c.sun_down")
            if to[0] == "unavailable":
                return _spec("c.unavailable", many, tail, who=who)
            if verb:
                return _spec("c." + verb, many, tail, who=who)
            return _spec("c.state", many, tail, who=who, state=to[0])
        if to:
            return _spec("c.states", many, tail, who=who, states=to)
        return _spec("c.other")
    if k == "num":
        lo, hi = n.get("above"), n.get("below")
        if lo is not None and hi is not None:
            return _spec("c.between", many, who=who, lo=_numv(ctx, lo, ids), hi=_numv(ctx, hi, ids))
        if lo is not None:
            return _spec("c.above", many, who=who, val=_numv(ctx, lo, ids))
        if hi is not None:
            return _spec("c.below", many, who=who, val=_numv(ctx, hi, ids))
        return _spec("c.other")
    if k == "time":
        def tv(x):
            return {"name": ctx.name(x)} if "." in x else {"at": x}
        wd = [_spec("tail.wd", wd=n["wd"])] if n.get("wd") else []
        a, b = n.get("after"), n.get("before")
        if a and b:
            return _spec("c.between_times", tail=wd, a=tv(a), b=tv(b))
        if a:
            return _spec("c.after_time", tail=wd, a=tv(a))
        if b:
            return _spec("c.before_time", tail=wd, b=tv(b))
        if n.get("wd"):
            return _spec("c.weekday", wd=n["wd"])
        return _spec("c.other")
    if k == "sun":
        parts = []
        if n.get("after"):
            parts.append(_spec("c.after_" + n["after"], tail=[_off_tail(n.get("after_off"))]))
        if n.get("before"):
            parts.append(_spec("c.before_" + n["before"], tail=[_off_tail(n.get("before_off"))]))
        if len(parts) == 1:
            return parts[0]
        return _spec("c.all", sub=parts) if parts else _spec("c.other")
    if k in ("and", "or", "not"):
        return _spec("c." + k, sub=[condition_spec(ctx, c) for c in n.get("c") or []])
    if k in ("tpl", "trigger", "device", "zone"):
        return _spec("c." + k)
    return _spec("c.other")


SERVICE_VERBS = {
    "turn_on": "on", "turn_off": "off", "toggle": "toggle", "open_cover": "open", "close_cover": "close",
    "stop_cover": "halt", "set_cover_position": "position", "open_valve": "open", "close_valve": "close", "lock": "lock",
    "unlock": "unlock", "open": "open", "set_temperature": "temp", "set_hvac_mode": "hvac", "set_preset_mode": "preset",
    "media_play": "play", "media_pause": "pause", "media_stop": "halt", "volume_set": "volume", "play_media": "media",
    "set_value": "set", "select_option": "set", "set_datetime": "set", "press": "press", "start": "start",
    "return_to_base": "home", "trigger": "start", "reload": "reload", "set_percentage": "set", "set_speed": "set",
}


def action_spec(ctx: Ctx, n: dict) -> dict:
    k = n.get("k")
    if k == "call":
        if n.get("notify"):
            return _spec("a.notify")
        svc = n.get("svc")
        if not svc:
            return _spec("a.call_tpl")
        dom, _, verb = svc.partition(".")
        ids = n.get("e") or []
        what = _names(ctx, ids)
        many = len(ids) > 1
        tail = [_spec("tail.area", areas=[ctx.areas.get(a, humanise("x." + a)) for a in n["area"]])] if n.get("area") else []
        if dom == "scene":
            return _spec("a.scene", many, tail, what=what) if what else _spec("a.scene_some", tail=tail)
        if dom == "script" and verb not in ("turn_on", "turn_off", "toggle", "reload"):
            return _spec("a.script", what=what or [humanise(svc)])
        if dom == "script" and verb == "turn_on":
            return _spec("a.script", many, what=what) if what else _spec("a.call_only", svc=svc)
        if dom == "automation" and verb == "trigger":
            return _spec("a.auto_start", many, what=what) if what else _spec("a.call_only", svc=svc)
        if not what and not tail:
            return _spec("a.call_only", svc=svc)
        key = SERVICE_VERBS.get(verb)
        if not what:
            return _spec("a.call_area", tail=tail, svc=svc)
        if key:
            return _spec("a." + key, many, tail, what=what)
        return _spec("a.call", many, tail, what=what, svc=svc)
    if k == "delay":
        return _spec("a.delay", dur=n["for"]) if n.get("for") is not None else _spec("a.delay_tpl")
    if k == "wait":
        tail = [_dur_tail("tail.timeout", n.get("for"))]
        if n.get("t"):
            return _spec("a.wait_for", tail=tail, sub=[trigger_spec(ctx, t) for t in n["t"]])
        return _spec("a.wait", tail=tail)
    if k == "branch":
        opts = []
        for o in n.get("opts") or []:
            opts.append(_spec("a.if", sub=[condition_spec(ctx, c) for c in o.get("c") or []],
                              then=[action_spec(ctx, a) for a in o.get("a") or []]))
        if n.get("else") is not None:
            opts.append(_spec("a.else", then=[action_spec(ctx, a) for a in n["else"]]))
        return _spec("a.branch", sub=opts)
    if k == "repeat":
        body = [action_spec(ctx, a) for a in n.get("a") or []]
        if n.get("n") is not None:
            return _spec("a.repeat_n", then=body, n=n["n"])
        if n.get("each"):
            return _spec("a.repeat_each", then=body)
        return _spec("a.repeat", sub=[condition_spec(ctx, c) for c in n.get("c") or []], then=body)
    if k == "parallel":
        return _spec("a.parallel", then=[action_spec(ctx, a) for a in n.get("a") or []])
    if k == "sequence":
        return _spec("a.sequence", then=[action_spec(ctx, a) for a in n.get("a") or []])
    if k == "stop":
        return _spec("a.stop_error" if n.get("error") else "a.stop")
    if k == "cond":
        return _spec("a.cond", sub=[condition_spec(ctx, c) for c in n.get("c") or []])
    if k in ("event", "device"):
        return _spec("a." + k)
    return _spec("a.other")


def where(ctx: Ctx, flow: dict) -> list:
    """Rooms of what the automation switches (area targets and the areas of its target entities)."""
    out = []
    for n in hub_flow.walk(flow.get("action")):
        for a in n.get("area") or []:
            name = ctx.areas.get(a)
            if name and name not in out:
                out.append(name)
        for e in n.get("e") or []:
            name = ctx.areas.get(ctx.area_of(e))
            if name and name not in out:
                out.append(name)
    return out[:MAX_NAMES]


def explain(ctx: Ctx, flow: dict | None) -> dict:
    if not isinstance(flow, dict):
        return {"exact": False, "when": [], "only": [], "then": [], "where": []}
    return {"exact": True, "cut": bool(flow.get("cut")),
            "when": [trigger_spec(ctx, n) for n in flow.get("trigger") or []],
            "only": [condition_spec(ctx, n) for n in flow.get("condition") or []],
            "then": [action_spec(ctx, n) for n in flow.get("action") or []],
            "where": where(ctx, flow)}


# ---------------------------------------------------------------------- findings
def _role_ids(flow: dict, part: str) -> set:
    out = set()
    for n in hub_flow.walk((flow or {}).get(part)):
        out.update(e for e in n.get("e") or () if isinstance(e, str))
    return out


def _target_ids(flow: dict) -> dict:
    """{entity: set of verbs} an automation switches ("on", "off", "toggle", ... from the service name)."""
    out = {}
    for n in hub_flow.walk((flow or {}).get("action")):
        if n.get("k") == "call" and n.get("svc") and not n.get("notify"):
            dom, verb = n["svc"].split(".", 1)
            if dom == "script" and verb not in ("turn_on", "turn_off", "toggle", "reload"):
                verb = "run"                        # "script.wake_up": the script is called, its name is no verb
            for e in n.get("e") or ():
                out.setdefault(e, set()).add(verb)
    return out


def _has_wait(flow: dict) -> bool:
    for n in hub_flow.walk((flow or {}).get("action")):
        if n.get("k") == "wait" or (n.get("k") == "delay" and (n.get("for") is None or n["for"] >= WAIT_MIN_S)):
            return True
    return False


def findings(ctx: Ctx, eid: str, row: dict, conflicts: dict) -> list:
    out = []
    flow = ctx.flows.get(eid)
    if flow:
        ids = hub_flow.entity_ids(flow)
        dead = sorted(e for e in ids if not ctx.known(e))
        if dead:
            out.append({"code": "dead", "ids": dead[:MAX_NAMES], "names": [humanise(e) for e in dead[:MAX_NAMES]], "n": len(dead)})
        unav = sorted(e for e in ids if ctx.known(e) and ctx.state(e) == "unavailable")
        if unav:
            out.append({"code": "unavailable", "ids": unav[:MAX_NAMES], "names": _names(ctx, unav[:MAX_NAMES]), "n": len(unav)})
        loop_ids = sorted(_role_ids(flow, "trigger") & set(_target_ids(flow)))
        if loop_ids:
            out.append({"code": "self_trigger", "ids": loop_ids[:MAX_NAMES], "names": _names(ctx, loop_ids[:MAX_NAMES])})
        if row.get("mode") == "single" and _has_wait(flow) and any(
                n.get("k") in ("state", "num", "event", "mqtt", "device", "pattern") for n in flow.get("trigger") or []):
            out.append({"code": "single_wait"})
    if row.get("runaway"):
        out.append({"code": "runaway", "n": row.get("peak") or 0})
    for c in conflicts.get(eid) or []:
        out.append({"code": "conflict", "ids": [c["entity"]], "names": [c["name"]], "other": c["other"],
                    "other_name": c["other_name"], "n": c["n"], "last": c["last"]})
    for ev in hubchain.modes_by_item(ctx).get(eid) or []:
        out.append({"code": "mode", "kind": ev["kind"], "mode": ev["mode"], "n": ev["n"], "last": ev["last"]})
    if row.get("loop"):
        out.append({"code": "loop"})
    if row["kind"] == "automation" and row.get("state") == "on" and (
            not row.get("last") or ctx.now - row["last"] > NEVER_DAYS * 86400):
        out.append({"code": "never", "days": NEVER_DAYS, "last": row.get("last")})
    if row.get("blocked"):
        out.append({"code": "blocked"})
    for f in out:
        f["level"] = LEVEL[f["code"]]
    out.sort(key=lambda f: FINDINGS.index(f["code"]))
    return out


# ---------------------------------------------------------------------- who switches what
_KIND_CACHE: dict = {}


def _cause_kind(by) -> str:
    got = _KIND_CACHE.get(by)
    if got is None:
        if by == "user":
            got = "user"
        elif isinstance(by, str) and privacy.domain_of(by) in ("automation", "script"):
            got = privacy.domain_of(by)
        else:
            got = "device"
        if len(_KIND_CACHE) < 20000:
            _KIND_CACHE[by] = got
    return got


def _changes(ctx: Ctx, since: float) -> list:
    return [c for c in ctx.entry.get("changes") or () if c[0] >= since]


def conflicts(ctx: Ctx, since: float | None = None) -> list:
    """Two automations/scripts setting the same entity to opposite states within ``CONFLICT_S`` seconds."""
    if since is None and getattr(ctx, "_conflicts", None) is not None:
        return ctx._conflicts
    out = _conflicts(ctx, ctx.now - hubbridge.CHANGE_KEEP_S if since is None else since)
    if since is None:
        ctx._conflicts = out
    return out


def _conflicts(ctx: Ctx, since: float) -> list:
    last_by = {}
    pairs = {}
    for t, eid, state, by in sorted(_changes(ctx, since), key=lambda c: c[0]):
        if _cause_kind(by) not in ("automation", "script"):
            if _cause_kind(by) == "user":
                last_by.pop(eid, None)         # a person in between: not a fight between two automations
            continue
        prev = last_by.get(eid)
        if prev and prev[1] != by and t - prev[0] <= CONFLICT_S and (prev[2], state) in OPPOSITE:
            key = (eid,) + tuple(sorted((prev[1], by)))
            rec = pairs.setdefault(key, {"entity": eid, "a": key[1], "b": key[2], "n": 0, "last": t})
            rec["n"] += 1
            rec["last"] = max(rec["last"], t)
        last_by[eid] = (t, by, state)
    out = []
    for rec in pairs.values():
        out.append(dict(rec, name=ctx.name(rec["entity"]), a_name=ctx.name(rec["a"]), b_name=ctx.name(rec["b"])))
    out.sort(key=lambda r: (-r["n"], -r["last"]))
    return out


def _conflicts_by_item(conf: list) -> dict:
    out = {}
    for c in conf:
        out.setdefault(c["a"], []).append({"entity": c["entity"], "name": c["name"], "other": c["b"], "other_name": c["b_name"],
                                           "n": c["n"], "last": c["last"]})
        out.setdefault(c["b"], []).append({"entity": c["entity"], "name": c["name"], "other": c["a"], "other_name": c["a_name"],
                                           "n": c["n"], "last": c["last"]})
    return out


def who(ctx: Ctx) -> dict:
    """Per entity: who switched it in the last 24 hours (automations, scripts, a person, the device itself) and which
    automations are configured to switch it; conflicts first."""
    rows = {}
    shown = {}
    for t, eid, state, by in _changes(ctx, ctx.now - hubbridge.CHANGE_KEEP_S):
        ok = shown.get(eid)
        if ok is None:
            ok = shown[eid] = privacy.domain_of(eid) in TIMELINE_DOMAINS
        if not ok and not by:
            continue
        r = rows.setdefault(eid, {"causes": {}, "n": 0, "last": 0})
        key = by if isinstance(by, str) else "-"
        c = r["causes"].setdefault(key, {"n": 0, "last": 0, "states": []})
        c["n"] += 1
        c["last"] = max(c["last"], t)
        if state not in c["states"] and len(c["states"]) < 4:
            c["states"].append(state)
        r["n"] += 1
        r["last"] = max(r["last"], t)
    planned = {}
    for item, flow in ctx.flows.items():
        for e, verbs in _target_ids(flow).items():
            planned.setdefault(e, []).append({"by": item, "name": ctx.name(item), "verbs": sorted(verbs)})
    conf = conflicts(ctx)
    conf_n = {}
    for c in conf:
        conf_n[c["entity"]] = conf_n.get(c["entity"], 0) + c["n"]
    out = []
    for eid in set(rows) | set(planned):
        r = rows.get(eid) or {"causes": {}, "n": 0, "last": None}
        causes = sorted(({"by": None if k == "-" else k, "kind": _cause_kind(None if k == "-" else k),
                          "name": ctx.name(k) if k not in ("-", "user") else None, "n": c["n"], "last": c["last"],
                          "states": c["states"]} for k, c in r["causes"].items()), key=lambda c: (-c["n"], c["kind"]))
        autos = {c["by"] for c in causes if c["kind"] in ("automation", "script")} | {p["by"] for p in planned.get(eid, [])}
        out.append(dict(ctx.ent_info(eid), causes=causes, planned=sorted(planned.get(eid, []), key=lambda p: p["name"].lower()),
                        n=r["n"], last=r["last"] or None, conflicts=conf_n.get(eid, 0), switchers=len(autos)))
    out.sort(key=lambda r: (-r["conflicts"], -r["switchers"], -r["n"], r["name"].lower()))
    return {"entities": out[:MAX_WHO], "total": len(out), "conflicts": conf[:50], "window_s": CONFLICT_S}


# ---------------------------------------------------------------------- timeline
def timeline(ctx: Ctx, hours: float = 24.0) -> list:
    """Who started what when: state changes with their cause (automation, script, a person - never who - or the device
    itself) and automation runs, newest first."""
    since = ctx.now - hours * 3600
    out = []
    for t, eid, state, by in reversed(ctx.entry.get("changes") or ()):   # newest first: only the last MAX_TIMELINE matter
        if len(out) >= MAX_TIMELINE:
            break
        if t < since:
            continue                                    # a late, old event (the ring is in the order things arrived)
        if privacy.domain_of(eid) not in TIMELINE_DOMAINS and not by:
            continue
        kind = _cause_kind(by)
        out.append({"t": t, "type": "change", "entity": eid, "name": ctx.name(eid), "state": state,
                    "area": ctx.areas.get(ctx.area_of(eid)), "by": by if kind in ("automation", "script") else None,
                    "by_name": ctx.name(by) if kind in ("automation", "script") else None, "kind": kind})
    for t, eid, n in ctx.entry.get("run_log") or ():
        if t >= since:
            out.append({"t": t, "type": "run", "entity": eid, "name": ctx.name(eid), "n": n, "kind": privacy.domain_of(eid)})
    out.sort(key=lambda e: -e["t"])
    return out[:MAX_TIMELINE]


# ---------------------------------------------------------------------- day plan
def _helper_time(ctx: Ctx, eid: str):
    st = ctx.state(eid)
    if isinstance(st, str) and len(st) >= 5 and st[2] == ":" and st[:2].isdigit() and st[3:5].isdigit():
        return st[:5]
    if isinstance(st, str) and len(st) >= 16 and st[10] in "T " and st[13] == ":":
        return st[11:16]
    return None


def dayplan(ctx: Ctx) -> dict:
    """Fixed times of the day: time triggers (also from time helpers), sun triggers placed with the hub's next sunrise
    and sunset, time and sun spans of conditions; rhythms (every 5 minutes) listed apart; runs of the last 24 hours."""
    sun = ctx.snap.get("sun") or {}
    runs = {}
    for t, eid, _n in ctx.entry.get("run_log") or ():
        if t >= ctx.now - 86400:
            runs.setdefault(eid, []).append(round(t, 1))
    items, rhythm, unplaced = [], [], []
    for eid, flow in sorted(ctx.flows.items()):
        if not eid.startswith("automation."):
            continue
        base = {"id": eid, "name": ctx.name(eid), "state": ctx.state(eid)}
        marks, spans = [], []
        for n in flow.get("trigger") or []:
            if n.get("k") == "time":
                for a in n.get("at") or []:
                    at = _helper_time(ctx, a) if "." in a else a
                    if at:
                        marks.append({"kind": "time", "at": at, "wd": n.get("wd"), "helper": ctx.name(a) if "." in a else None})
                    else:
                        unplaced.append(dict(base, kind="time", helper=ctx.name(a)))
            elif n.get("k") == "sun":
                ev = n.get("ev") or "sunset"
                ts = sun.get("rise" if ev == "sunrise" else "set")
                if ts:
                    marks.append({"kind": "sun", "ev": ev, "off": n.get("off") or 0, "ts": ts + (n.get("off") or 0)})
                else:
                    unplaced.append(dict(base, kind="sun", ev=ev, off=n.get("off") or 0))
            elif n.get("k") == "pattern":
                rhythm.append(dict(base, spec=trigger_spec(ctx, n)))
        for n in hub_flow.walk(flow.get("condition")):
            if n.get("k") == "time" and (n.get("after") or n.get("before")):
                a = n.get("after")
                b = n.get("before")
                a = _helper_time(ctx, a) if a and "." in a else a
                b = _helper_time(ctx, b) if b and "." in b else b
                spans.append({"kind": "time", "from": a, "to": b, "wd": n.get("wd")})
            elif n.get("k") == "sun" and (n.get("after") or n.get("before")):
                def at(ev, off):
                    ts = sun.get("rise" if ev == "sunrise" else "set") if ev else None
                    return ts + (off or 0) if ts else None
                spans.append({"kind": "sun", "from_ts": at(n.get("after"), n.get("after_off")),
                                "to_ts": at(n.get("before"), n.get("before_off")), "after": n.get("after"), "before": n.get("before")})
        if marks or spans:
            items.append(dict(base, marks=marks, spans=spans, ran=sorted(runs.get(eid, []))[-MAX_RAN:]))
    items.sort(key=lambda i: min([m.get("at") or "99" for m in i["marks"]] + ["99"]))
    return {"items": items, "rhythm": rhythm, "unplaced": unplaced, "sun": {"rise": sun.get("rise"), "set": sun.get("set")},
            "admin": ctx.admin, "known": bool(ctx.flows)}


# ---------------------------------------------------------------------- devices per room
def rooms(ctx: Ctx, radio_state: dict | None = None, net_of: dict | None = None) -> list:
    radio_state = radio_state or {}
    net_of = net_of or {}
    by_area = {}
    ents_of = {}
    for e in ctx.ents.values():
        if e.get("cat") or privacy.domain_of(e["entity_id"]) in ROOM_SKIP_DOMAINS:
            continue
        ents_of.setdefault(e.get("device_id"), []).append(e)
    touched = {}
    for item, flow in ctx.flows.items():
        for e in _target_ids(flow):
            a = ctx.area_of(e)
            if a:
                touched.setdefault(a, set()).add(item)
    for d in ctx.devs.values():
        if d.get("service"):
            continue
        ents = ents_of.get(d["id"]) or []
        if not ents:
            continue
        area = d.get("area") if d.get("area") in ctx.areas else None
        st = radio_state.get(d["id"]) or {}
        rows = sorted(ents, key=lambda e: (privacy.domain_of(e["entity_id"]) not in ("light", "switch", "cover", "climate", "lock", "media_player"),
                                           e["entity_id"]))[:MAX_ROOM_ENTITIES]
        by_area.setdefault(area, []).append({
            "id": d["id"], "name": d.get("name") or d["id"], "radio": d.get("radio"), "fresh": st.get("fresh"),
            "network_device": net_of.get(d["id"]),
            "entities": [{"id": e["entity_id"], "name": e.get("name") or humanise(e["entity_id"]), "domain": privacy.domain_of(e["entity_id"]),
                          "state": ctx.state(e["entity_id"]), "unit": e.get("unit"), "dc": e.get("dc")} for e in rows],
            "more": max(0, len(ents) - MAX_ROOM_ENTITIES),
            "unavailable": sum(1 for e in ents if ctx.state(e["entity_id"]) == "unavailable")})
    out = []
    order = list(ctx.areas) + [None]
    for a in order:
        devs = by_area.get(a)
        if not devs and not touched.get(a):
            continue
        devs = sorted(devs or [], key=lambda d: d["name"].lower())
        out.append({"id": a, "name": ctx.areas.get(a) if a else None, "devices": devs,
                    "on": sum(1 for d in devs for e in d["entities"] if e["state"] in ("on", "open", "playing", "unlocked", "heat", "cool")),
                    "unavailable": sum(d["unavailable"] for d in devs),
                    "automations": sorted(({"id": i, "name": ctx.name(i)} for i in touched.get(a, ())), key=lambda x: x["name"].lower())})
    return out


# ---------------------------------------------------------------------- dependency board
def board(ctx: Ctx, eid: str) -> dict | None:
    """Sources on the left, consumers on the right. For an automation or script: what starts and checks it, what it
    switches. For any other entity: what sets it (automations, scripts, a person, the device itself) and what reads it."""
    if not privacy.valid_entity_id(eid) or privacy.is_private(eid) or not (ctx.known(eid) or eid in ctx.flows):
        return None
    dom = privacy.domain_of(eid)
    left, right = [], []

    def pad(e, role, **kw):
        return dict(ctx.ent_info(e), role=role, **kw)

    if dom in ("automation", "script") and eid in ctx.flows:
        flow = ctx.flows[eid]
        seen = set()
        for role, part in (("starts", "trigger"), ("checks", "condition")):
            for e in sorted(_role_ids(flow, part)):
                if (e, role) not in seen:
                    seen.add((e, role))
                    left.append(pad(e, role))
        for e, verbs in sorted(_target_ids(flow).items()):
            right.append(pad(e, "switches", verbs=sorted(verbs)))
        for e in sorted(set(hub_flow.entity_ids({"action": flow.get("action")})) - set(_target_ids(flow))):
            right.append(pad(e, "reads"))
        # who calls this script / automation
        for item, f in sorted(ctx.flows.items()):
            if item != eid and eid in _target_ids(f):
                left.append(pad(item, "calls"))
    else:
        for item, f in sorted(ctx.flows.items()):
            t = _target_ids(f)
            if eid in t:
                left.append(pad(item, "sets", verbs=sorted(t[eid])))
            if eid in _role_ids(f, "trigger"):
                right.append(pad(item, "starts"))
            elif eid in _role_ids(f, "condition") or eid in hub_flow.entity_ids({"action": f.get("action")}) and eid not in t:
                right.append(pad(item, "checks"))
        seen_by = set()
        for _t, e, _s, by in _changes(ctx, ctx.now - hubbridge.CHANGE_KEEP_S):
            if e == eid and _cause_kind(by) in ("user", "device") and _cause_kind(by) not in seen_by:
                seen_by.add(_cause_kind(by))
        for kind in sorted(seen_by):
            left.append({"id": None, "name": None, "domain": None, "role": kind, "state": None, "missing": False,
                         "unavailable": False, "item": False})
    # references the bridge only found in templates or through related items (weaker)
    r = ctx.refs.get(eid) or {}
    for e in r.get("text") or []:
        if not any(p["id"] == e for p in left + right):
            left.append(pad(e, "template"))
    center = ctx.ent_info(eid)
    ent = ctx.ents.get(eid) or {}
    dev = ctx.devs.get(ent.get("device_id")) or {}
    center.update({"device": dev.get("name"), "device_id": dev.get("id"), "kind": dom, "exact": eid in ctx.flows or dom not in ("automation", "script")})
    return {"center": center, "left": left[:60], "right": right[:60], "admin": ctx.admin, "known": bool(ctx.flows)}


# ---------------------------------------------------------------------- health of the hub (findings, never healing)
def health(ctx: Ctx, radio: dict | None = None) -> dict:
    entries = ctx.snap.get("entries")
    problems = [e for e in entries or [] if e.get("state") in PROBLEM_ENTRY_STATES]
    not_loaded = [e for e in entries or [] if e.get("state") == "not_loaded"]
    by_dom, by_plat = {}, {}
    unknown = 0
    for e in ctx.ents.values():
        st = ctx.state(e["entity_id"])
        if st == "unknown":
            unknown += 1
        if st != "unavailable":
            continue
        dom = privacy.domain_of(e["entity_id"])
        g = by_dom.setdefault(dom, {"domain": dom, "n": 0, "items": []})
        g["n"] += 1
        if len(g["items"]) < MAX_UNAVAILABLE:
            dev = ctx.devs.get(e.get("device_id")) or {}
            g["items"].append({"id": e["entity_id"], "name": e.get("name") or humanise(e["entity_id"]),
                               "device": dev.get("name"), "area": ctx.areas.get(ctx.area_of(e["entity_id"]))})
        plat = e.get("platform") or "-"
        by_plat[plat] = by_plat.get(plat, 0) + 1
    groups = sorted(by_dom.values(), key=lambda g: (-g["n"], g["domain"]))
    silent = sum(1 for s in (radio or {}).get("state", {}).values() if s.get("fresh") == "silent")
    found = []
    for p in problems:
        found.append({"code": "integration", "level": "warn", "domain": p["domain"], "state": p["state"], "n": p["n"]})
    for g in groups:
        found.append({"code": "unavailable", "level": "warn" if g["n"] >= 3 else "info", "domain": g["domain"], "n": g["n"]})
    if silent:
        found.append({"code": "radio_silent", "level": "warn", "n": silent})
    if ctx.snap.get("limited"):
        found.append({"code": "limited", "level": "info"})
    return {"entries_known": entries is not None, "integrations": {"problems": problems, "not_loaded": not_loaded,
            "loaded": sum(e["n"] for e in entries or [] if e.get("state") == "loaded"), "total": sum(e["n"] for e in entries or [])},
            "unavailable": groups, "unavailable_total": sum(g["n"] for g in groups), "unknown": unknown,
            "platforms": sorted(({"platform": p, "n": n} for p, n in by_plat.items()), key=lambda x: (-x["n"], x["platform"]))[:10],
            "radio_silent": silent, "findings": found, "entities": len(ctx.ents)}


# ---------------------------------------------------------------------- the list
def rows(ctx: Ctx, only: str | None = None) -> list:
    hour = hubbridge.runs_last_hour(ctx.entry, ctx.now)
    peak = hubbridge.busiest_hour(ctx.entry, ctx.now - hubbridge.HOUR, ctx.now)
    day = hubbridge.runs_between(ctx.entry, ctx.now - 86400, ctx.now)
    blocked = {b.get("entity_id") for b in ctx.deps.get("blocked") or []}
    in_loop = {e for c in ctx.deps.get("cycles") or [] for e in c}
    conf = _conflicts_by_item(conflicts(ctx))
    out = []
    for eid, item in ctx.items.items():
        if only is not None and eid != only:
            continue
        rec = (ctx.entry.get("runs") or {}).get(eid) or {}
        flow = ctx.flows.get(eid)
        st = ctx.state(eid) if ctx.state(eid) is not None else item.get("state")
        row = {"id": eid, "name": ctx.name(eid), "kind": privacy.domain_of(eid), "state": st, "mode": item.get("mode"),
               "last": max(x for x in (item.get("last"), rec.get("last"), 0) if x is not None) or None,
               "runs_hour": hour.get(eid, 0), "runs_day": day.get(eid, 0), "peak": (peak.get(eid) or (0, 0))[0],
               "runaway": (peak.get(eid) or (0, 0))[0] >= hubbridge.BUSY_PER_HOUR, "loop": eid in in_loop,
               "blocked": eid in blocked, "exact": flow is not None, "traceable": bool(item.get("id"))}
        row["findings"] = findings(ctx, eid, row, conf)
        row["areas"] = where(ctx, flow) if flow else []
        row["triggers"] = sorted({n.get("k") for n in (flow or {}).get("trigger") or []})
        ent_ids = hub_flow.entity_ids(flow) if flow else set()
        row["search"] = " ".join(sorted({ctx.name(e) for e in ent_ids}))[:600]
        out.append(row)
    out.sort(key=lambda r: (-sum(1 for f in r["findings"] if f["level"] == "warn"), r["kind"] != "automation", r["name"].lower()))
    return out


def chain_view(ctx: Ctx, entity: str | None = None, device: str | None = None, item: str | None = None) -> dict:
    """Effect chains of the last 24 hours for one entity, one hub device (all its entities) or one automation/script."""
    ents = None
    if entity:
        ents = [entity]
    elif device:
        ents = [e for e, info in ctx.ents.items() if info.get("device_id") == device]
    return {"chains": hubchain.chains(ctx, entities=ents, item=item), "runs_summary": hubchain.summary(ctx.entry, ctx.now),
            "window_s": hubchain.WINDOW_S, "max_depth": hubchain.MAX_DEPTH}


def tab(ctx: Ctx, radio: dict | None = None) -> dict:
    """The list page with its counts and filters."""
    rs = rows(ctx)
    counts = {c: 0 for c in FINDINGS}
    for r in rs:
        for f in r["findings"]:
            counts[f["code"]] += 1
    areas = sorted({a for r in rs for a in r["areas"]}, key=str.lower)
    h = health(ctx, radio)
    return {"connected": bool(ctx.snap), "admin": ctx.admin, "exact": len(ctx.flows), "t": ctx.snap.get("t"),
            "rows": rs, "counts": counts, "areas": areas,
            "totals": {"automations": sum(1 for r in rs if r["kind"] == "automation"), "scripts": sum(1 for r in rs if r["kind"] == "script"),
                       "on": sum(1 for r in rs if r["kind"] == "automation" and r["state"] == "on"),
                       "off": sum(1 for r in rs if r["kind"] == "automation" and r["state"] == "off"),
                       "with_findings": sum(1 for r in rs if r["findings"]),
                       "conflicts": len(conflicts(ctx)), "health": sum(1 for f in h["findings"] if f["level"] == "warn")}}


def detail(ctx: Ctx, eid: str) -> dict | None:
    """One automation or script: plain words, the flow with its entities, findings, runs and its board."""
    if eid not in ctx.items:
        return None
    row = (rows(ctx, eid) or [None])[0]
    flow = ctx.flows.get(eid)
    parts = {}
    if flow:
        for part, fn in (("trigger", trigger_spec), ("condition", condition_spec), ("action", action_spec)):
            parts[part] = [{"k": n.get("k"), "spec": fn(ctx, n), "svc": n.get("svc"),
                            "ents": [ctx.ent_info(e) for e in sorted(hub_flow.entity_ids({part: [n]}))]} for n in flow.get(part) or []]
    refs = ctx.refs.get(eid) or {}
    hours = {}
    rec = (ctx.entry.get("runs") or {}).get(eid) or {}
    for b, c in (rec.get("hours") or {}).items():
        hours[int(b) * hubbridge.HOUR] = c
    return {"row": row, "explain": explain(ctx, flow), "flow": parts, "cut": bool((flow or {}).get("cut")),
            "chains": hubchain.chains(ctx, item=eid)[:20], "runs_summary": hubchain.summary(ctx.entry, ctx.now),
            "related": [ctx.ent_info(e) for e in (refs.get("related") or [])[:40]],
            "hours": sorted(([t, n] for t, n in hours.items() if t >= ctx.now - 86400 - hubbridge.HOUR), key=lambda x: x[0]),
            "admin": ctx.admin}
