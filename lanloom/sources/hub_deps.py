"""Dependencies and load protection of the hub bridge: pure functions, no network.

* ``refs_from_config`` reads an automation or script configuration (as the hub returns it) and keeps only the entity
  ids it names, per role (trigger / condition / action). No configuration text, template or value leaves this module.
* ``cycles`` finds strongly connected components (Tarjan, iterative): scripts that call each other in a loop. The hub
  resolves such scripts recursively when asked what is related to them and can run into its recursion limit, so the
  bridge never asks about them (lesson from the private map's bridge).
* ``Budget`` is a token bucket per minute (on-demand traces, configuration reads, related-item lookups).
* ``Blocklist`` remembers items the hub answered with an error and leaves them alone for a while (24 h, doubling up to
  a week; a changed configuration lifts the block; three timeouts in a row count as one error).
"""
from __future__ import annotations

import hashlib
import json
import random
import re

ROLES = ("trigger", "condition", "action")
ROLE_KEYS = {"trigger": "trigger", "triggers": "trigger", "condition": "condition", "conditions": "condition",
             "action": "action", "actions": "action", "sequence": "action"}
CALL_KEYS = ("service", "action")                    # "service: script.x" / "action: script.x" calls a script directly
EID_IN_TEXT = re.compile(r"(?<![\w.])([a-z_]{1,64}\.[a-z0-9_]{1,190})(?![\w.(])")
MAX_REFS = 200
MAX_DEPTH = 40
MAX_NODES = 20000

BLOCK_S = 86400.0
BLOCK_MAX_S = 7 * 86400.0
TIMEOUTS_PER_BLOCK = 3
BLOCK_CAP = 2000


def _ids(value) -> list:
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def refs_from_config(config, known) -> dict:
    """{role: [entity ids]} plus ``text`` (ids only found inside templates, weaker) - only ids the hub knows."""
    out = {r: set() for r in ROLES}
    text = set()
    budget = [MAX_NODES]

    def walk(node, role, depth):
        budget[0] -= 1
        if budget[0] < 0 or depth > MAX_DEPTH:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                r = ROLE_KEYS.get(k, role) if depth <= 1 or role is None else role
                if k == "entity_id" and r:
                    out[r].update(e for e in _ids(v) if e in known)
                elif k in CALL_KEYS and r and isinstance(v, str) and v.startswith("script.") and v in known:
                    out[r].add(v)
                else:
                    walk(v, r, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, role, depth + 1)
        elif isinstance(node, str) and role and ("{{" in node or "{%" in node):
            text.update(e for e in EID_IN_TEXT.findall(node[:20000]) if e in known)

    walk(config, None, 0)
    res = {r: sorted(out[r])[:MAX_REFS] for r in ROLES}
    res["text"] = sorted(text - set().union(*out.values()))[:MAX_REFS]
    return res


def related_ids(result) -> list:
    """Answer of a related-items lookup -> the entity ids in it (automations, scripts, entities); nothing else."""
    out = set()
    if isinstance(result, dict):
        for k in ("entity", "automation", "script"):
            for e in result.get(k) or []:
                if isinstance(e, str) and EID_IN_TEXT.fullmatch(e):
                    out.add(e)
    return sorted(out)[:MAX_REFS]


def script_calls(refs_by_script: dict) -> dict:
    """{script.x: {script.y, …}} from the action references of every script."""
    graph = {}
    for eid, refs in refs_by_script.items():
        graph[eid] = {e for e in (refs or {}).get("action", ()) if e.startswith("script.")}
    return graph


def cycles(graph: dict) -> list:
    """Strongly connected components with two or more nodes or a self loop (Tarjan, iterative). graph: {node: iterable}."""
    index, low, on, stack, out, n = {}, {}, set(), [], [], 0
    for start in sorted(graph):
        if start in index:
            continue
        index[start] = low[start] = n
        n += 1
        stack.append(start)
        on.add(start)
        work = [(start, iter(sorted(graph.get(start, ()))))]
        while work:
            v, it = work[-1]
            pushed = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = n
                    n += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(graph.get(w, ())))))
                    pushed = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if pushed:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1 or v in graph.get(v, ()):
                    out.append(sorted(comp))
    return sorted(out)


def fingerprint(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8", "replace")).hexdigest()[:16]


def jitter(seconds: float, rng=random) -> float:
    """Spread renewals (x0.75 … x1.25) so that not every item falls due in the same round."""
    return seconds * (0.75 + 0.5 * rng.random())


class Budget:
    """Token bucket: at most ``per_minute`` takes per minute, bursts up to ``burst``."""

    def __init__(self, per_minute: int, burst: int | None = None, mono: float = 0.0):
        self.rate = max(1, int(per_minute)) / 60.0
        self.cap = float(burst if burst is not None else max(1, int(per_minute)))
        self.tokens = self.cap
        self.t = mono
        self.waits = 0

    def _fill(self, mono: float) -> None:
        if mono > self.t:
            self.tokens = min(self.cap, self.tokens + (mono - self.t) * self.rate)
        self.t = max(self.t, mono)

    def take(self, mono: float) -> bool:
        self._fill(mono)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        self.waits += 1
        return False

    def ready(self, mono: float) -> bool:
        self._fill(mono)
        return self.tokens >= 1.0

    def wait_s(self, mono: float) -> float:
        self._fill(mono)
        return 0.0 if self.tokens >= 1.0 else (1.0 - self.tokens) / self.rate


class Blocklist:
    """Items the hub answered with an error: left alone with growing pauses."""

    def __init__(self):
        self.items: dict = {}          # key -> {"n", "until", "fp", "code", "timeouts"}

    def blocked(self, key: str, now: float, fp: str | None = None) -> bool:
        e = self.items.get(key)
        if not e or e["until"] <= now:
            return False
        if fp is not None and e.get("fp") is not None and e["fp"] != fp:
            self.items.pop(key, None)  # the configuration changed: try again
            return False
        return True

    def fail(self, key: str, now: float, code: str = "", fp: str | None = None, timeout: bool = False) -> bool:
        """Count an error answer (or a timeout). True when the item is blocked now."""
        e = self.items.get(key) or {"n": 0, "until": 0.0, "fp": fp, "code": "", "timeouts": 0}
        if timeout:
            e["timeouts"] += 1
            if e["timeouts"] < TIMEOUTS_PER_BLOCK:
                self.items[key] = e
                return False
            e["timeouts"] = 0
        e["n"] += 1
        e["until"] = now + min(BLOCK_MAX_S, BLOCK_S * 2 ** (e["n"] - 1))
        e["fp"], e["code"] = fp, str(code or "")[:40]
        self.items[key] = e
        if len(self.items) > BLOCK_CAP:            # keep the newest blocks
            for k in sorted(self.items, key=lambda k: self.items[k]["until"])[:len(self.items) - BLOCK_CAP]:
                self.items.pop(k, None)
        return True

    def ok(self, key: str) -> None:
        self.items.pop(key, None)

    def active(self, now: float) -> list:
        return sorted(({"key": k, "until": round(e["until"], 1), "n": e["n"], "code": e["code"]}
                       for k, e in self.items.items() if e["until"] > now), key=lambda x: x["key"])
