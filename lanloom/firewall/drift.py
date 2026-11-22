"""Rule changes made outside Lanloom: a small state per controller, the difference between two states, and an event
for the journal (``fw_drift``) that the overview, the change list and the incident file ("changed before") read.

Why: when something breaks, the first question is "what did we change?". The editor writes ``fw_change`` for what it
did itself; a rule changed directly on the device or in the controller's own interface was only noticed by the nightly
report of the device family, once per report, and never reached the event journal.

State (small, per controller, never an address): zones (name, kind), network -> zone, rules (id, name, action, from/to as
zone, on/off, position, a fingerprint of what the rule does - addresses, ports, protocol, states, schedule, also the
contents of groups it points to), port forwards (name, port, on/off; never the destination), the zone pairs that may
talk, and the mode of the intrusion detection (None when the controller does not tell: not read is never "off").
Hit counters and timestamps are not part of the state. Names that contain an address are made unreadable.

A new state counts only when it arrived in two reads in a row (half-read states would give "rule gone" and right
after "rule new"); the time of the event is the first read with the new state. The first state of a controller is the
base state and gives no events. While the editor runs, or when it finished a change since the last accepted state, the
new state is taken over silently: that change is already in the journal as ``fw_change``.

Events (journal kind ``fw_drift``): ``sub`` and ``level`` (``danger`` | ``warn`` | ``info``) plus the names the
sentence needs; the texts live in the translation files (``fwd.sub.<sub>``)::

    pair_open / pair_open_in / pair_partial / pair_closed / pair_narrowed    zone pair opened (``_in``: from the internet
                                                                             to the inside or the gateway) or closed
    rule_new / rule_new_off / rule_gone / rule_off / rule_on / rule_moved / rule_changed / rule_action
    fwd_new / fwd_new_off / fwd_gone / fwd_off / fwd_on / fwd_port           port forwards
    ids                                                                      intrusion detection switched
    net_zone / zone_new / zone_gone
    auto                                                                     rules the controller makes itself: a count only
    bulk                                                                     more changes than fit: a count
"""
import hashlib
import json
import logging
import re
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("lanloom")

SETTING = "fw_drift"
KIND = "fw_drift"
CONFIRM_S = 30                  # the same new state in two reads at least this far apart (reads come every few minutes)
MAX_EVENTS = 30                 # per change; the rest is counted ("bulk")
MAX_ZONES = 80
MAX_RULES = 3000
MAX_FORWARDS = 500
LEVELS = ("danger", "warn", "info")
INSIDE = ("internal", "gateway", "vpn")             # "inside": from the internet to there is an alarm
IDS_RANK = {"off": 0, "ids": 1, "on": 2, "ips": 2}
ACTIONS = ("allow", "block", "reject")
EFFECT = ("action", "proto", "ports", "return_traffic", "states", "scheduled", "src", "dst")

_IP4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b")
_IP6 = re.compile(r"(?<![0-9A-Za-z])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:/\d{1,3})?(?![0-9A-Za-z])")
_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b")


def clean_name(s, length: int = 60) -> str:
    """A name for a sentence: control characters, markup and addresses (IPv4, IPv6, hardware address) removed, shortened."""
    t = re.sub(r"[\x00-\x1f\x7f<>]", " ", str(s or ""))
    t = _MAC.sub("…", _IP6.sub("…", _IP4.sub("…", t)))
    t = re.sub(r"\s+", " ", t).strip()
    return (t[:length].rstrip() + "…") if len(t) > length else (t or "—")


def _int(v) -> int:
    try:
        return int(v) if not isinstance(v, bool) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _effect(p: dict) -> str:
    """Fingerprint of what a rule does (not its name, switch, position or counters)."""
    d = {k: p.get(k) for k in EFFECT}
    raw = json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _zone_kind(z: dict) -> str:
    return str(z.get("system") or z.get("kind") or "")[:20]


def _pairs(zones: list, allow: list, pols: list) -> dict:
    """"a>b" -> 2 (open: a rule or the allowed pairs let everything through) | 1 (partly open: a rule of its own lets some
    traffic through). Closed pairs are left out."""
    ids = [z["id"] for z in zones]
    full = {(a, b) for a, b in allow if isinstance(a, str) and isinstance(b, str)}
    partial = set()
    for p in pols:
        if p.get("builtin") or not p.get("enabled", True) or p.get("action") != "allow":
            continue
        partial.add(((p.get("src") or {}).get("zone"), (p.get("dst") or {}).get("zone")))
    out = {}
    for a in ids:
        for b in ids:
            if a == b:
                continue
            if (a, b) in full or ("*", b) in full or (a, "*") in full or ("*", "*") in full:
                out["%s>%s" % (a, b)] = 2
            elif (a, b) in partial or ("*", b) in partial or (a, "*") in partial or ("*", "*") in partial:
                out["%s>%s" % (a, b)] = 1
    return out


def build_stand(state) -> Optional[dict]:
    """A neutral configuration -> the small state. None when it has neither zones nor rules nor forwards (nothing to compare)."""
    if not isinstance(state, dict):
        return None
    zones = [z for z in (state.get("zones") or []) if isinstance(z, dict) and isinstance(z.get("id"), str)][:MAX_ZONES]
    pols = [p for p in (state.get("policies") or []) if isinstance(p, dict) and isinstance(p.get("id"), str)][:MAX_RULES]
    fwds = [f for f in (state.get("port_forwards") or []) if isinstance(f, dict) and isinstance(f.get("id"), str)][:MAX_FORWARDS]
    if not zones and not pols and not fwds:
        return None
    zmap = {z["id"]: {"n": clean_name(z.get("name"), 40), "k": _zone_kind(z)} for z in zones}
    mgmt = {n.get("zone") for n in state.get("networks") or [] if isinstance(n, dict) and n.get("management")}
    for zid in mgmt:
        if zid in zmap:
            zmap[zid]["m"] = 1
    nets = {n["id"]: {"n": clean_name(n.get("name"), 40), "z": n.get("zone")} for n in state.get("networks") or []
            if isinstance(n, dict) and isinstance(n.get("id"), str)}
    ordered = sorted(pols, key=lambda p: (_int(p.get("order")), p["id"]))
    rules = {}
    for i, p in enumerate(ordered):
        rules[p["id"]] = {"n": clean_name(p.get("name")), "a": p.get("action") if p.get("action") in ACTIONS else "other", "on": bool(p.get("enabled", True)), "i": i,
                          "v": (p.get("src") or {}).get("zone"), "z": (p.get("dst") or {}).get("zone"), "au": bool(p.get("builtin")),
                          "h": _effect(p)}
    fw = {f["id"]: {"n": clean_name(f.get("name")), "p": re.sub(r"[^0-9,\-]", "", str(f.get("wan_port") or ""))[:40], "on": bool(f.get("enabled", True))}
          for f in fwds}
    allow = [list(x) for x in state.get("allow") or [] if isinstance(x, (list, tuple)) and len(x) == 2]
    return {"v": 1, "zones": zmap, "nets": nets, "rules": rules, "pairs": _pairs(zones, allow, pols), "fwd": fw, "ids": _ids_mode(state)}


def _ids_mode(state: dict) -> Optional[str]:
    ids = state.get("ids")
    if not isinstance(ids, dict):
        return None
    mode = str(ids.get("mode") or "").lower()
    if mode in IDS_RANK:
        return mode
    if "enabled" in ids and isinstance(ids["enabled"], bool):
        return "on" if ids["enabled"] else "off"
    return None


def digest(stand: dict) -> str:
    return hashlib.sha1(json.dumps(stand, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------- the difference
def _lcs_moved(a: list, b: list) -> list:
    """Elements of ``b`` outside a longest common subsequence of ``a`` and ``b``: the ones that were moved (a rule moved to
    the top gives one entry, not one for every rule that moved down)."""
    n, m = len(a), len(b)
    t = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            t[i][j] = t[i + 1][j + 1] + 1 if a[i] == b[j] else max(t[i + 1][j], t[i][j + 1])
    i = j = 0
    kept = set()
    while i < n and j < m:
        if a[i] == b[j]:
            kept.add(b[j])
            i += 1
            j += 1
        elif t[i + 1][j] >= t[i][j + 1]:
            i += 1
        else:
            j += 1
    return [x for x in b if x not in kept]


def _zname(*stands, zid) -> str:
    for s in stands:
        z = (s.get("zones") or {}).get(zid)
        if z:
            return clean_name(z["n"], 40)
    return "?"


def _ev(sub: str, level: str, **kw) -> dict:
    return dict({"sub": sub, "level": level}, **kw)


def diff(old: dict, new: dict) -> list:
    """Events from state ``old`` to state ``new`` (both from ``build_stand``): danger first, then warn, then info."""
    ev = []
    zo, zn = old.get("zones") or {}, new.get("zones") or {}
    for zid, z in zn.items():
        if zid not in zo:
            ev.append(_ev("zone_new", "info", name=clean_name(z["n"], 40)))
    for zid, z in zo.items():
        if zid not in zn:
            ev.append(_ev("zone_gone", "info", name=clean_name(z["n"], 40)))
    for nid, n in (new.get("nets") or {}).items():
        o = (old.get("nets") or {}).get(nid)
        if o and o.get("z") != n.get("z"):
            ev.append(_ev("net_zone", "warn", name=clean_name(n["n"], 40), zone=_zname(new, old, zid=n.get("z")), was=_zname(old, new, zid=o.get("z"))))
    # zone pairs
    po, pn = old.get("pairs") or {}, new.get("pairs") or {}
    for k in sorted(set(po) | set(pn)):
        r0, r1 = po.get(k, 0), pn.get(k, 0)
        if r0 == r1:
            continue
        a, b = k.split(">", 1)
        names = {"from": _zname(new, old, zid=a), "to": _zname(new, old, zid=b)}
        if r1 > r0:
            za, zb = zn.get(a) or zo.get(a) or {}, zn.get(b) or zo.get(b) or {}
            danger = za.get("k") == "external" and (zb.get("k") in INSIDE or zb.get("m"))
            if danger and r1 == 2:
                ev.append(_ev("pair_open_in", "danger", **names))
            else:
                ev.append(_ev("pair_open" if r1 == 2 else "pair_partial", "danger" if danger else "warn", **names))
        else:
            ev.append(_ev("pair_closed" if r1 == 0 else "pair_narrowed", "info", **names))
    # rules (the controller's own rules only count)
    ro, rn = old.get("rules") or {}, new.get("rules") or {}
    auto = 0
    for rid, r in rn.items():
        o = ro.get(rid)
        if r.get("au") or (o and o.get("au")):
            if not o or any(o.get(k) != r.get(k) for k in ("h", "on", "a")):
                auto += 1
            continue
        base = {"name": clean_name(r["n"]), "from": _zname(new, old, zid=r.get("v")), "to": _zname(new, old, zid=r.get("z")), "action": r.get("a")}
        allow = r.get("a") == "allow"
        if not o:
            ev.append(_ev("rule_new" if r.get("on") else "rule_new_off", "warn" if allow and r.get("on") else "info", **base))
            continue
        if o.get("on") and not r.get("on"):
            ev.append(_ev("rule_off", "info", **base))
        elif not o.get("on") and r.get("on"):
            ev.append(_ev("rule_on", "warn" if allow else "info", **base))
        if o.get("h") != r.get("h"):
            if o.get("a") != r.get("a"):
                ev.append(_ev("rule_action", "warn" if allow else "info", was=o.get("a"), **base))
            else:
                ev.append(_ev("rule_changed", "warn" if allow else "info", **base))
    for rid, o in ro.items():
        if rid in rn:
            continue
        if o.get("au"):
            auto += 1
            continue
        ev.append(_ev("rule_gone", "info", name=clean_name(o["n"]), action=o.get("a"), **{"from": _zname(old, new, zid=o.get("v")), "to": _zname(old, new, zid=o.get("z"))}))
    # position inside one zone pair: only rules in both states with the same pair
    per_pair = {}
    for rid in rn:
        a_, b_ = ro.get(rid), rn[rid]
        if not a_ or a_.get("au") or b_.get("au") or (a_.get("v"), a_.get("z")) != (b_.get("v"), b_.get("z")):
            continue
        per_pair.setdefault((b_.get("v"), b_.get("z")), []).append(rid)
    for (v, z), ids in sorted(per_pair.items(), key=lambda x: str(x[0])):
        if len(ids) < 2:
            continue
        before = sorted(ids, key=lambda i: (ro[i].get("i") or 0, i))
        after = sorted(ids, key=lambda i: (rn[i].get("i") or 0, i))
        if before == after:
            continue
        for rid in _lcs_moved(before, after):
            ev.append(_ev("rule_moved", "info", name=clean_name(rn[rid]["n"]), action=rn[rid].get("a"), **{"from": _zname(new, old, zid=v), "to": _zname(new, old, zid=z)}))
    if auto:
        ev.append(_ev("auto", "info", n=auto))
    # port forwards
    fo, fn = old.get("fwd") or {}, new.get("fwd") or {}
    for fid, f in fn.items():
        o = fo.get(fid)
        base = {"name": clean_name(f["n"]), "port": f.get("p") or "—"}
        if not o:
            ev.append(_ev("fwd_new" if f.get("on") else "fwd_new_off", "warn" if f.get("on") else "info", **base))
        elif o.get("on") and not f.get("on"):
            ev.append(_ev("fwd_off", "info", **base))
        elif not o.get("on") and f.get("on"):
            ev.append(_ev("fwd_on", "warn", **base))
        elif o.get("p") != f.get("p"):
            ev.append(_ev("fwd_port", "warn" if f.get("on") else "info", was=o.get("p") or "—", **base))
    for fid, o in fo.items():
        if fid not in fn:
            ev.append(_ev("fwd_gone", "info", name=clean_name(o["n"]), port=o.get("p") or "—"))
    # intrusion detection
    i0, i1 = old.get("ids"), new.get("ids")
    if i0 is not None and i1 is not None and IDS_RANK.get(i0, 0) != IDS_RANK.get(i1, 0):
        ev.append(_ev("ids", "warn" if IDS_RANK.get(i1, 0) < IDS_RANK.get(i0, 0) else "info", mode=i1, was=i0))
    ev.sort(key=lambda e: LEVELS.index(e["level"]))
    if len(ev) > MAX_EVENTS:
        rest = len(ev) - (MAX_EVENTS - 1)
        ev = ev[:MAX_EVENTS - 1] + [_ev("bulk", "info", n=rest)]
    return ev


# ---------------------------------------------------------------------------------------------- the watcher
class Drift:
    """One per site (a site has its own store and journal). ``journal``: callable -> the event journal;
    ``own_change``: callable (since, until) -> True when the editor ran or finished a change in that time."""

    def __init__(self, store, journal: Callable, own_change: Optional[Callable] = None, clock: Callable[[], float] = time.time):
        self.store = store
        self.journal = journal
        self.own_change = own_change
        self.clock = clock
        self.lock = threading.Lock()
        self.cand = {}                       # key -> {"dg", "t", "stand"} (memory only: a restart starts the check again)

    def _load(self) -> dict:
        d = self.store.get_setting(SETTING, {})
        return d if isinstance(d, dict) else {}

    def feed(self, key: str, name: str, family: Optional[str], state, now: Optional[float] = None, keep: Optional[set] = None) -> list:
        """One read of a controller's configuration. Returns the events that became valid with it (usually none)."""
        now = self.clock() if now is None else float(now)
        stand = build_stand(state)
        if stand is None:
            return []
        dg = digest(stand)
        with self.lock:
            data = self._load()
            if keep is not None:
                gone = [k for k in data if k not in keep]
                for k in gone:
                    data.pop(k, None)
                    self.cand.pop(k, None)
            cur = data.get(key)
            if cur is None:
                data[key] = {"stand": stand, "dg": dg, "t": round(now, 1), "base": round(now, 1)}
                self.cand.pop(key, None)
                self.store.set_setting(SETTING, data)
                return []
            if dg == cur["dg"]:
                self.cand.pop(key, None)
                if keep is not None and gone:
                    self.store.set_setting(SETTING, data)
                return []
            c = self.cand.get(key)
            if not c or c["dg"] != dg:
                self.cand[key] = {"dg": dg, "t": now}
                return []
            if now - c["t"] < CONFIRM_S:
                return []
            if self._editor_running():
                return []                    # the new state may be a halfway state of a plan that is being written
            events = []
            if not self._is_own(c["t"], now):
                events = diff(cur["stand"], stand)
            data[key] = {"stand": stand, "dg": dg, "t": round(now, 1), "base": cur.get("base", cur["t"])}
            self.cand.pop(key, None)
            self.store.set_setting(SETTING, data)
        self._journal(events, key, name, family, c["t"])
        return events

    def _editor_running(self) -> bool:
        try:
            return bool(self.own_change and self.own_change(None, None))
        except Exception:
            return False

    def _is_own(self, since: float, until: float) -> bool:
        try:
            return bool(self.own_change and self.own_change(since, until))
        except Exception:
            return False

    def _journal(self, events: list, key: str, name: str, family: Optional[str], t: float) -> None:
        j = self.journal() if self.journal else None
        if j is None:
            return
        for e in events:
            try:
                j.add(KIND, dict(e, controller=clean_name(name, 40), integ=key, family=str(family or "")[:20]), t=t)
            except Exception:
                log.exception("journalling a firewall change failed")

    def base_time(self, key: str) -> Optional[float]:
        d = self._load().get(key)
        return d.get("base") if d else None


def own_change_probe(editor, journal: Callable) -> Callable:
    """The ``own_change`` callable of ``Drift``: ``(None, None)`` asks "is the editor writing right now?", ``(since, until)``
    asks "did the editor finish a change in that time?" (``fw_change`` rows in the journal)."""
    def probe(since, until) -> bool:
        if since is None:
            return editor.running() is not None
        j = journal()
        return j is not None and bool(j.between(since - 600, until, ("fw_change",)))
    return probe


def describe(data: dict) -> dict:
    """An event row of the journal -> ``{"key", "vars"}`` for the incident file (the web client translates; variables ending
    in ``_key`` are translation keys)."""
    d = data or {}
    sub = str(d.get("sub") or "")
    vars_ = {k: str(d[k])[:80] for k in ("name", "from", "to", "zone", "was", "port", "mode") if d.get(k) not in (None, "")}
    if d.get("action"):
        vars_["action_key"] = "fwd.act." + str(d["action"])[:12]
    if sub == "rule_action" and d.get("was"):
        vars_["was_key"] = "fwd.act." + str(d["was"])[:12]
        vars_.pop("was", None)
    if sub == "ids":
        vars_["mode_key"] = "fwd.ids." + str(d.get("mode") or "off")
        vars_["was_key"] = "fwd.ids." + str(d.get("was") or "off")
        vars_.pop("mode", None)
        vars_.pop("was", None)
    if sub in ("auto", "bulk"):
        vars_["n"] = _int(d.get("n"))
    return {"key": "fwd.sub." + sub, "vars": vars_}
