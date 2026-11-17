"""The flow of an automation or script, reduced to what the Automations tab needs - pure functions, no network.

``flow_from_config`` reads a configuration as the hub returns it and keeps only its structure: per step the kind, the
entity ids it names (valid, not private), state words from a fixed list, times of day, numeric thresholds, durations,
weekdays, sun events with their offset and the name of the action it calls (``light.turn_on``). Aliases,
descriptions, trigger ids, templates, service data, notification targets, event data and every free text are dropped
here, before anything is stored. A step that holds a template is only marked (``tpl``).

Node keys (short on purpose, the flow travels with every dependency frame):

``k``     kind (``TRIGGER_KINDS`` / ``CONDITION_KINDS`` / ``ACTION_KINDS``)
``e``     entity ids                       ``to`` / ``from``   state words (``STATE_WORDS``) or numbers
``above`` / ``below``  number or entity id  ``for``  seconds    ``at``   times "HH:MM" or entity ids (time helpers)
``after`` / ``before`` time "HH:MM", entity id or a sun event   ``wd``   weekdays (mon ... sun)
``ev``    ``sunrise`` / ``sunset``           ``off``  offset in seconds (signed)
``every`` ``{"h"|"m"|"s": "*/5" ...}``      ``svc``  action called    ``area``  area ids of a target
``notify`` the step sends a message (who to is never kept)          ``c``  nested conditions
``a``     nested actions                    ``opts`` branches ``[{"c": [...], "a": [...]}]``  ``else``  actions
``tpl``   a template decides here           ``n``    number of times (repeat)
"""
from __future__ import annotations

import re

from .. import hub_privacy as privacy

MAX_DEPTH = 5
MAX_NODES = 240
MAX_IDS = 40

ENTITY_RX = privacy.ENTITY_RX
SERVICE_RX = re.compile(r"\A[a-z0-9_]{1,64}\.[a-z0-9_]{1,64}\Z")
HHMM_RX = re.compile(r"\A(\d{1,2}):(\d{2})(?::(\d{2}))?\Z")
DUR_RX = re.compile(r"\A(-?)(\d{1,3}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\Z")
PATTERN_RX = re.compile(r"\A(?:\*|/?\d{1,2})\Z")
AREA_RX = re.compile(r"\A[a-z0-9_]{1,64}\Z")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SUN_EVENTS = ("sunrise", "sunset")
# states with a fixed meaning: the only words a flow keeps (anything else - an option of a select helper, a name - is
# replaced by "_", "some value")
STATE_WORDS = frozenset((
    "on", "off", "open", "closed", "opening", "closing", "locked", "unlocked", "locking", "unlocking", "jammed",
    "playing", "paused", "idle", "standby", "buffering", "unavailable", "unknown", "heat", "cool", "auto", "heat_cool",
    "dry", "fan_only", "heating", "cooling", "armed_away", "armed_home", "armed_night", "armed_vacation", "disarmed",
    "triggered", "pending", "arming", "above_horizon", "below_horizon", "docked", "cleaning", "returning", "error",
    "detected", "clear", "charging", "discharging", "full", "low", "home", "not_home", "true", "false"))
TRIGGER_KINDS = ("state", "num", "time", "pattern", "sun", "start", "event", "tpl", "mqtt", "webhook", "device", "zone",
                 "calendar", "tag", "other")
CONDITION_KINDS = ("state", "num", "time", "sun", "tpl", "and", "or", "not", "trigger", "device", "zone", "other")
ACTION_KINDS = ("call", "delay", "wait", "branch", "repeat", "parallel", "sequence", "stop", "event", "cond", "device",
                "other")
PLATFORMS = {"state": "state", "numeric_state": "num", "time": "time", "time_pattern": "pattern", "sun": "sun",
             "homeassistant": "start", "event": "event", "template": "tpl", "mqtt": "mqtt", "webhook": "webhook",  # identifier: platform name of the start trigger
             "device": "device", "zone": "zone", "geo_location": "zone", "calendar": "calendar", "tag": "tag",
             "conversation": "other", "persistent_notification": "other"}
CONDITIONS = {"state": "state", "numeric_state": "num", "time": "time", "sun": "sun", "template": "tpl", "and": "and",
              "or": "or", "not": "not", "trigger": "trigger", "device": "device", "zone": "zone"}
NOTIFY_DOMAINS = ("notify", "persistent_notification")


def _is_tpl(v) -> bool:
    return isinstance(v, str) and ("{{" in v or "{%" in v)


def _ids(value, shown=frozenset()) -> list:
    raw = [v.strip() for v in value.split(",")] if isinstance(value, str) else value if isinstance(value, list) else []
    out = []
    for v in raw:
        if isinstance(v, str) and ENTITY_RX.match(v) and not privacy.is_private(v, shown) and v not in out:
            out.append(v)
    return out[:MAX_IDS]


def _private_ids(value, shown=frozenset()) -> bool:
    raw = [v.strip() for v in value.split(",")] if isinstance(value, str) else value if isinstance(value, list) else []
    return any(isinstance(v, str) and privacy.is_private(v, shown) for v in raw)


def seconds(value) -> int | None:
    """Duration ("0:10:00", 600, {"minutes": 10}) -> seconds; None for templates or anything unreadable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if 0 <= value <= 366 * 86400 else None
    if isinstance(value, str):
        m = DUR_RX.match(value.strip())
        if not m:
            return int(value) if value.strip().isdigit() and int(value) <= 366 * 86400 else None
        s = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4) or 0)
        return -s if m.group(1) else s
    if isinstance(value, dict):
        total = 0.0
        for k, mult in (("days", 86400), ("hours", 3600), ("minutes", 60), ("seconds", 1), ("milliseconds", 0.001)):
            v = value.get(k)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None
            total += v * mult
        return int(total) if 0 <= total <= 366 * 86400 else None
    return None


def _offset(value) -> int | None:
    s = seconds(value)
    if s is None and isinstance(value, str) and value.startswith("-"):
        s = seconds(value[1:])
        s = -s if s is not None else None
    return s if s is not None and abs(s) <= 86400 else None


def _hhmm(value, shown=frozenset()):
    """A time of day "HH:MM" or the entity id of a time helper; None otherwise."""
    if isinstance(value, str):
        m = HHMM_RX.match(value.strip())
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            return "%02d:%02d" % (int(m.group(1)), int(m.group(2)))
        ids = _ids(value, shown)
        if len(ids) == 1:
            return ids[0]
    return None


def _word(value):
    """A state word from the fixed list, a number, or "_" (some value) - never free text."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        v = value.strip()
        if v.lower() in STATE_WORDS:
            return v.lower()
        try:
            return float(v) if "." in v else int(v)
        except ValueError:
            return "_"
    return None


def _words(value) -> list | None:
    if value is None:
        return None
    vals = value if isinstance(value, list) else [value]
    out = []
    for v in vals[:6]:
        w = _word(v)
        if w is not None and w not in out:
            out.append(w)
    return out or None


def _number(value, shown=frozenset()):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        ids = _ids(value, shown)
        if len(ids) == 1:
            return ids[0]
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _weekdays(value) -> list | None:
    vals = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    out = [d for d in WEEKDAYS if d in vals]
    return out or None


def _target_ids(step: dict, shown) -> tuple:
    """Entity ids and area ids a service step aims at (``target``, ``data`` or the step itself); nothing else."""
    ids, areas = [], []
    for holder in (step, step.get("target"), step.get("data"), step.get("data_template")):
        if not isinstance(holder, dict):
            continue
        for e in _ids(holder.get("entity_id"), shown):
            if e not in ids:
                ids.append(e)
        a = holder.get("area_id")
        for v in ([a] if isinstance(a, str) else a if isinstance(a, list) else []):
            if isinstance(v, str) and AREA_RX.match(v) and v not in areas:
                areas.append(v)
    return ids[:MAX_IDS], areas[:MAX_IDS]


class _Reader:
    def __init__(self, shown):
        self.shown = shown
        self.budget = MAX_NODES

    def take(self) -> bool:
        self.budget -= 1
        return self.budget >= 0

    # ---------------------------------------------------------------- triggers
    def trigger(self, t) -> dict | None:
        if not isinstance(t, dict) or not self.take():
            return None
        plat = t.get("platform", t.get("trigger"))
        k = PLATFORMS.get(plat, "other") if isinstance(plat, str) else "other"
        node = {"k": k}
        if k in ("state", "num"):
            node["e"] = _ids(t.get("entity_id"), self.shown)
            if _private_ids(t.get("entity_id"), self.shown) and not node["e"]:
                node["k"] = "zone"                       # presence of a person: a "someone arrives" kind, no ids
                node.pop("e")
                return node
        if k == "state":
            for f in ("to", "from"):
                w = _words(t.get(f))
                if w:
                    node[f] = w
            if t.get("attribute"):
                node["attr"] = True                      # an attribute change, which one is not kept
        elif k == "num":
            for f in ("above", "below"):
                v = _number(t.get(f), self.shown)
                if v is not None:
                    node[f] = v
            if t.get("value_template") is not None:
                node["tpl"] = True
        elif k == "time":
            at = t.get("at")
            times = [_hhmm(a, self.shown) for a in (at if isinstance(at, list) else [at])]
            node["at"] = [x for x in times if x][:12]
            if not node["at"] or any(_is_tpl(a) for a in (at if isinstance(at, list) else [at])):
                node["tpl"] = node.get("tpl") or not node["at"]
            wd = _weekdays(t.get("weekday"))
            if wd:
                node["wd"] = wd
        elif k == "pattern":
            every = {}
            for f, key in (("hours", "h"), ("minutes", "m"), ("seconds", "s")):
                v = t.get(f)
                v = str(v) if isinstance(v, int) and not isinstance(v, bool) else v
                if isinstance(v, str) and PATTERN_RX.match(v.strip()):
                    every[key] = v.strip()
            node["every"] = every
        elif k == "sun":
            if t.get("event") in SUN_EVENTS:
                node["ev"] = t["event"]
            off = _offset(t.get("offset"))
            if off:
                node["off"] = off
        elif k == "start":
            node["ev"] = t.get("event") if t.get("event") in ("start", "shutdown") else "start"
        elif k == "tpl":
            node["tpl"] = True
        d = seconds(t.get("for"))
        if d is not None and k in ("state", "num", "tpl", "device"):
            node["for"] = d
        elif t.get("for") is not None and k in ("state", "num", "tpl", "device"):
            node["tpl"] = True
        return node

    # ---------------------------------------------------------------- conditions
    def condition(self, c, depth: int) -> dict | None:
        if depth > MAX_DEPTH or not self.take():
            return None
        if isinstance(c, str):
            return {"k": "tpl", "tpl": True} if _is_tpl(c) else None
        if not isinstance(c, dict):
            return None
        kind = c.get("condition")
        k = CONDITIONS.get(kind, "other") if isinstance(kind, str) else "other"
        node = {"k": k}
        if k in ("state", "num"):
            node["e"] = _ids(c.get("entity_id"), self.shown)
            if _private_ids(c.get("entity_id"), self.shown) and not node["e"]:
                return {"k": "zone"}
        if k == "state":
            w = _words(c.get("state"))
            if w:
                node["to"] = w
            d = seconds(c.get("for"))
            if d is not None:
                node["for"] = d
        elif k == "num":
            for f in ("above", "below"):
                v = _number(c.get(f), self.shown)
                if v is not None:
                    node[f] = v
            if c.get("value_template") is not None:
                node["tpl"] = True
        elif k == "time":
            for f in ("after", "before"):
                v = _hhmm(c.get(f), self.shown)
                if v:
                    node[f] = v
            wd = _weekdays(c.get("weekday"))
            if wd:
                node["wd"] = wd
        elif k == "sun":
            for f in ("after", "before"):
                if c.get(f) in SUN_EVENTS:
                    node[f] = c[f]
                    off = _offset(c.get(f + "_offset"))
                    if off:
                        node[f + "_off"] = off
        elif k == "tpl":
            node["tpl"] = True
        elif k in ("and", "or", "not"):
            subs = c.get("conditions")
            node["c"] = self.conditions(subs if isinstance(subs, list) else [subs], depth + 1)
        return node

    def conditions(self, items, depth: int) -> list:
        out = []
        for c in (items if isinstance(items, list) else [items] if items is not None else []):
            n = self.condition(c, depth)
            if n:
                out.append(n)
        return out

    # ---------------------------------------------------------------- actions
    def action(self, a, depth: int) -> dict | None:
        if depth > MAX_DEPTH or not isinstance(a, dict) or not self.take():
            return None
        svc = a.get("action", a.get("service"))
        if isinstance(svc, str):
            if not SERVICE_RX.match(svc):
                return {"k": "call", "tpl": True}
            dom = svc.split(".", 1)[0]
            if dom in NOTIFY_DOMAINS:
                return {"k": "call", "svc": dom + ".send", "notify": True}      # who gets it is never kept
            ids, areas = _target_ids(a, self.shown)
            node = {"k": "call", "svc": svc, "e": ids}
            if dom == "script" and svc not in ("script.turn_on", "script.turn_off", "script.toggle", "script.reload"):
                node["e"] = [svc] if ENTITY_RX.match(svc) else []
            if areas:
                node["area"] = areas
            if any(_is_tpl(v) for v in (a.get("entity_id"), (a.get("target") or {}).get("entity_id") if isinstance(a.get("target"), dict) else None)):
                node["tpl"] = True
            return node
        if "delay" in a:
            d = seconds(a.get("delay"))
            return {"k": "delay", "for": d} if d is not None else {"k": "delay", "tpl": True}
        if "wait_template" in a or "wait_for_trigger" in a:
            node = {"k": "wait"}
            d = seconds(a.get("timeout"))
            if d is not None:
                node["for"] = d
            if "wait_for_trigger" in a:
                trig = a["wait_for_trigger"]
                node["t"] = [n for n in (self.trigger(x) for x in (trig if isinstance(trig, list) else [trig])) if n]
            else:
                node["tpl"] = True
            return node
        if "choose" in a or "if" in a:
            node = {"k": "branch", "opts": []}
            if "if" in a:
                node["opts"].append({"c": self.conditions(a.get("if"), depth + 1), "a": self.actions(a.get("then"), depth + 1)})
                if a.get("else") is not None:
                    node["else"] = self.actions(a.get("else"), depth + 1)
            else:
                ch = a.get("choose")
                for opt in (ch if isinstance(ch, list) else [ch])[:12]:
                    if isinstance(opt, dict):
                        node["opts"].append({"c": self.conditions(opt.get("conditions"), depth + 1),
                                             "a": self.actions(opt.get("sequence"), depth + 1)})
                if a.get("default") is not None:
                    node["else"] = self.actions(a.get("default"), depth + 1)
            return node
        if "repeat" in a and isinstance(a["repeat"], dict):
            r = a["repeat"]
            node = {"k": "repeat", "a": self.actions(r.get("sequence"), depth + 1)}
            if isinstance(r.get("count"), int) and not isinstance(r.get("count"), bool):
                node["n"] = max(0, min(r["count"], 10000))
            elif r.get("count") is not None or r.get("while") is not None or r.get("until") is not None:
                node["c"] = self.conditions(r.get("while", r.get("until")), depth + 1)
            elif r.get("for_each") is not None:
                node["each"] = True
            return node
        if "parallel" in a:
            return {"k": "parallel", "a": self.actions(a.get("parallel"), depth + 1)}
        if "sequence" in a:
            return {"k": "sequence", "a": self.actions(a.get("sequence"), depth + 1)}
        if "stop" in a:
            return {"k": "stop", "error": bool(a.get("error"))}
        if "event" in a:
            return {"k": "event"}                                # name and data of the event are not kept
        if "condition" in a:
            c = self.condition(a, depth + 1)
            return {"k": "cond", "c": [c] if c else []}
        if "scene" in a:
            return {"k": "call", "svc": "scene.turn_on", "e": _ids(a.get("scene"), self.shown)}
        if "device_id" in a and "domain" in a:
            return {"k": "device"}
        if "variables" in a:
            return None                                          # values of variables are never kept
        return {"k": "other"}

    def actions(self, items, depth: int) -> list:
        out = []
        for a in (items if isinstance(items, list) else [items] if items is not None else []):
            n = self.action(a, depth)
            if n:
                out.append(n)
        return out


def flow_from_config(config, shown=frozenset()) -> dict:
    """{"trigger": [...], "condition": [...], "action": [...]} - the structure of one automation (scripts: actions)."""
    cfg = config if isinstance(config, dict) else {}
    r = _Reader(shown)
    trig = cfg.get("triggers", cfg.get("trigger"))
    conds = cfg.get("conditions", cfg.get("condition"))
    acts = cfg.get("actions", cfg.get("action", cfg.get("sequence")))
    out = {"trigger": [n for n in (r.trigger(t) for t in (trig if isinstance(trig, list) else [trig] if trig else [])) if n],
           "condition": r.conditions(conds, 1),
           "action": r.actions(acts, 1)}
    if r.budget < 0:
        out["cut"] = True                                        # bigger than MAX_NODES: the rest is not shown
    return out


def walk(nodes):
    """Every node of a flow part, nested ones included (depth first)."""
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        yield from walk(n.get("c"))
        yield from walk(n.get("a"))
        yield from walk(n.get("t"))
        yield from walk(n.get("else"))
        for o in n.get("opts") or []:
            yield from walk(o.get("c"))
            yield from walk(o.get("a"))


def entity_ids(flow: dict) -> set:
    out = set()
    for part in ("trigger", "condition", "action"):
        for n in walk((flow or {}).get(part)):
            out.update(e for e in n.get("e") or () if isinstance(e, str))
            for f in ("above", "below", "after", "before"):
                if isinstance(n.get(f), str) and "." in n[f]:
                    out.add(n[f])
            out.update(a for a in n.get("at") or () if isinstance(a, str) and "." in a)
    return out
