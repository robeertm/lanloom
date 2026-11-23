"""Incidents (incident files): faults bundled into episodes, with a probable root, a kind and a short file each.

Sources, all Lanloom's own (nothing here talks to the network):

* the **availability slots** (lanloom/availability.py): every device and connection in 5-minute slots. A run of slots with
  failed looks is an *episode* of that subject; runs at most ``FLAP_GAP_SLOTS`` good slots apart belong to one episode.
  Slots without a look are unknown, never "down" - Lanloom not running is never a fault of a device.
* the **event journal** (lanloom/nightly/journal.py): the exact times a device stopped / started answering refine the
  slot edges (``dev_offline`` / ``dev_back``), the internet uplink adds its own episodes (``wan_down`` / ``wan_up``) and
  Lanloom's own updates (``app_stop`` / ``app_start`` with another version) become *maintenance* incidents.
* the **dependency graph** (lanloom/failure.py) bundles episodes that started together: a member that sits in the
  failure chain of another member hangs on it (``sure`` or ``likely``, with the reason of the edge); what is left are
  the roots. Several unrelated roots at the same time point at what they share upstream (``likely``, reason ``shared``).
* **a maintenance window** of the notifications (N1, ``notify.maintenance_at``) and the user's own mark on a file.

Kinds: ``maintenance`` (window, own update, or marked by a person), ``flapping`` (``FLAP_RUNS`` or more runs in one
episode), ``blip`` (one short run up to ``BLIP_S``), ``outage`` (the rest). Phones and computers come and go: they never
start an incident on their own, they only join one as a member.

Storage: one row per incident (``incident``) so the 90-day calendar outlives the 35 days of slots. The recent part
(``WINDOW_S`` and everything still open) is computed again on every collection - a row whose root changes is replaced,
never doubled. Older rows stay as they are. Reading never waits on the network; the collection is cached ``CACHE_S``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Callable, Optional

from . import availability as av

log = logging.getLogger("lanloom")

SLOT = av.SLOT_S
WINDOW_S = 48 * 3600            # the part computed again on every collection
CONTEXT_S = 6 * 3600            # slots read before the window (an episode that started earlier keeps its start)
FLAP_GAP_SLOTS = 2              # up to two good slots (10 minutes) between bad runs: the same episode
FLAP_RUNS = 3                   # runs in one episode from which it counts as flapping
BLIP_S = 600                    # one run up to this long: a blip
BUNDLE_S = 600                  # episodes that start this close together are looked at as one event
REFINE_BEFORE_S = SLOT
REFINE_AFTER_S = 2 * SLOT
KEEP_DAYS = 120                 # rows kept (the calendar shows 90 days)
CALENDAR_DAYS = 90
BEFORE_S = 2 * 3600             # "what changed before": the time looked back
BEFORE_FW_S = 24 * 3600         # firmware jumps and rule changes made outside Lanloom are looked for a day back
BEFORE_RULES_MAX = 6            # rule changes shown (danger first)
CACHE_S = 60.0
MAX_LIST = 400
MAX_MEMBERS = 60
KINDS = ("outage", "flapping", "blip", "maintenance")
NO_ROOT_KINDS = ("phone", "computer")
MARK_SETTING = "incident_marks"
STATE = "incident_state"
NOTE_MAX = 160
SCHEMA = """
CREATE TABLE IF NOT EXISTS incident (id TEXT PRIMARY KEY, start REAL NOT NULL, "end" REAL, kind TEXT NOT NULL, root TEXT NOT NULL,
    data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS incident_start ON incident(start);
"""
_DOWN = bytes(1 if b & 15 else 0 for b in range(256))   # slot byte -> 1 when a look failed
CHANGE_KINDS = ("dev_new", "dev_gone", "fw_change", "app_start", "src_fail", "wan_switch")


REFULL_S = 120                  # the whole slot range is computed again at most this often (when the graph grew a lot)
REFULL_SHARE = 0.10


def _grown(old, new) -> bool:
    """The dependency graph changed by more than a tenth since the last full collection (nodes or edges)."""
    if not old or not new:
        return bool(new) and not old
    return any(abs(b - a) > max(3, a * REFULL_SHARE) for a, b in zip(old, new))


# ---------------------------------------------------------------------- pure parts (fixed series in the tests)
def runs_of(cells, gap: int = FLAP_GAP_SLOTS) -> list:
    """[[first slot index, end index (exclusive), runs]]: bad runs at most ``gap`` good slots apart are one episode."""
    out, i, n = [], 0, len(cells)
    while i < n:
        if cells[i] & 15:
            j = i
            while j < n and cells[j] & 15:
                j += 1
            if out and i - out[-1][1] <= gap and all(cells[k] >> 4 for k in range(out[-1][1], i)):
                out[-1][1] = j
                out[-1][2] += 1
            else:
                out.append([i, j, 1])
            i = j
        else:
            i += 1
    return out


def episodes(subject: str, cells, start_slot: int, now: float) -> list:
    """Episodes of one subject: [{"subject", "from", "to", "open", "runs"}] (timestamps; ``to`` None while open)."""
    now_slot = int(now // SLOT)
    out = []
    for a, b, n in runs_of(cells):
        # still going: the run reaches now, or no look found the subject up after it (slots without a look are not "back")
        is_open = start_slot + b >= now_slot or not any(c >> 4 for c in cells[b:])
        out.append({"subject": subject, "from": float((start_slot + a) * SLOT), "to": None if is_open else float((start_slot + b) * SLOT),
                    "open": is_open, "runs": n})
    return out


def kind_of(ep_runs: int, dur: float, is_open: bool, maintenance: bool) -> str:
    if maintenance:
        return "maintenance"
    if ep_runs >= FLAP_RUNS:
        return "flapping"
    if not is_open and ep_runs == 1 and dur <= BLIP_S:
        return "blip"
    return "outage"


def group_by_time(eps: list, gap: float = BUNDLE_S) -> list:
    """Episodes sorted by start, grouped when each starts at most ``gap`` after the first of its group."""
    groups = []
    for ep in sorted(eps, key=lambda e: (e["from"], e["subject"])):
        if groups and ep["from"] - groups[-1][0]["from"] <= gap:
            groups[-1].append(ep)
        else:
            groups.append([ep])
    return groups


def bundle(group: list, down: Callable, up: Callable, can_root: Callable) -> list:
    """Bundle one time group. ``down(node)`` -> {node: (stage, cert, prev, why)} (the failure chain), ``up(node)`` the same
    upwards, ``can_root(node)`` False for phones and computers. -> [{"root", "root_cert", "root_why", "observed",
    "members": [{"node", "cert", "stage", "why", "ep"}], "eps"}]"""
    nodes = {}
    for ep in group:
        nodes.setdefault(ep["node"], ep)
    raw = {n: down(n) for n in nodes}
    # a member that went down clearly before a part cannot have been taken down by it
    chains = {n: {m: rec for m, rec in raw[n].items() if m not in nodes or nodes[m]["from"] >= nodes[n]["from"] - SLOT} for n in nodes}
    roots = [n for n in nodes if not any(n in chains[o] for o in nodes if o != n)]
    hang = {}
    for n in nodes:
        if n in roots:
            continue
        best = None
        for r in roots:
            rec = chains[r].get(n)
            if rec and (best is None or (rec[1] == "sure" and best[1][1] != "sure") or (rec[1] == best[1][1] and rec[0] < best[1][0])):
                best = (r, rec)
        if best is None:                           # in the chain of a member that is itself not a root: follow it up
            for o in nodes:
                if o != n and n in chains[o]:
                    best = (o, chains[o][n])
                    break
        hang[n] = best
    # a member that hangs on a non-root ends up under that one's root
    def top(n, seen=()):
        h = hang.get(n)
        if h is None or h[0] in seen:
            return n
        return top(h[0], seen + (n,))
    out = {}
    for r in roots:
        out[r] = {"root": r, "root_cert": "sure", "root_why": None, "observed": True, "members": [], "eps": [nodes[r]]}
    for n, h in hang.items():
        r = top(n)
        if r not in out:
            out[r] = {"root": r, "root_cert": "sure", "root_why": None, "observed": True, "members": [], "eps": [nodes[r]]}
        rec = h[1] if h else ("", "likely", "", None)
        out[r]["members"].append({"node": n, "stage": rec[0], "cert": rec[1], "why": rec[3], "ep": nodes[n]})
        out[r]["eps"].append(nodes[n])
    bundles = list(out.values())
    # several roots at once with nothing in common on the map yet: what they share upstream is the probable root
    lone = [b for b in bundles if not b["members"] and can_root(b["root"])]
    if len(lone) >= 2:
        ups = [up(b["root"]) for b in lone]
        common = set(ups[0])
        for u in ups[1:]:
            common &= set(u)
        common -= set(nodes)
        if common:
            pick = min(common, key=lambda c: (max(u[c][0] for u in ups), c))
            merged = {"root": pick, "root_cert": "likely", "root_why": {"key": "shared", "cert": "likely", "vars": {"n": len(lone)}},
                      "observed": False, "members": [], "eps": []}
            for b, u in zip(lone, ups):
                rec = u[pick]
                merged["members"].append({"node": b["root"], "stage": rec[0], "cert": "likely", "why": rec[3], "ep": b["eps"][0]})
                merged["members"].extend(b["members"])
                merged["eps"].extend(b["eps"])
            bundles = [b for b in bundles if b not in lone] + [merged]
    return [b for b in bundles if can_root(b["root"]) or not b["observed"] or b["members"]]


def incident_id(root: str, start: float) -> str:
    return "i" + hashlib.sha1(("%s|%d" % (root, int(start // SLOT))).encode()).hexdigest()[:11]


def day_key(ts: float, tz=None) -> str:
    lt = time.localtime(ts) if tz is None else tz(ts)
    return "%04d-%02d-%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday)


# ---------------------------------------------------------------------- the store and the reading side
class Incidents:
    def __init__(self, core, clock: Callable[[], float] = time.time):
        self.core = core
        self.store = core.store
        self.clock = clock
        self.lock = threading.RLock()
        self._at = 0.0
        self._ver = None
        self.version = 0
        with self.store.transaction() as tx:
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    tx._db.execute(stmt)

    # ------------------------------------------------------------------ helpers over the core
    def _journal(self):
        return getattr(getattr(self.core, "reports", None), "journal", None)

    def _names(self) -> dict:
        out = {}
        for d in self.core.store.devices(include_hidden=True):
            data = d.get("data") or {}
            out["dev:" + d["id"]] = {"title": d.get("user_title") or d.get("title") or data.get("name") or d.get("ip") or d["id"],
                                     "kind": d.get("user_kind") or d.get("kind") or "unknown",
                                     "hidden": bool(d.get("hidden")), "ip": d.get("ip") or "", "self": bool(data.get("is_self"))}
        for r in self.core.store.query("SELECT id, kind, name FROM integrations"):
            out["src:int:%d" % r["id"]] = {"title": r["name"] or r["kind"], "kind": "connection", "integ_kind": r["kind"]}
        return out

    @staticmethod
    def node_of(subject: str) -> str:
        if subject.startswith("dev:"):
            return subject[4:]
        if subject.startswith("src:"):
            return subject[4:]
        return subject

    @staticmethod
    def subject_of(node: str) -> str:
        if node.startswith("int:"):
            return "src:" + node
        if node in ("internet", "lanloom"):
            return node
        return "dev:" + node

    def _maint(self, t: float, node: str) -> Optional[dict]:
        n = getattr(self.core, "notify", None)
        if n is None:
            return None
        try:
            return n.maintenance_at(t, node)
        except Exception:
            return None

    def marks(self) -> dict:
        return self.store.get_setting(MARK_SETTING, {}) or {}

    # ------------------------------------------------------------------ collection
    def collect(self, now: Optional[float] = None, force: bool = False) -> int:
        """Compute the recent incidents again and store them. Returns the number of incidents in the window."""
        now = self.clock() if now is None else now
        avail = self.core.availability
        key = (avail.version, len(self.marks()))
        with self.lock:
            if not force and now - self._at < CACHE_S and self._ver == key:
                return -1
            row = self.store.one('SELECT MIN(start) AS s FROM incident WHERE "end" IS NULL')
            win = now - WINDOW_S
            if row and row["s"] is not None:
                win = min(win, float(row["s"]))
            state = self.store.get_setting(STATE, {}) or {}
            size = self._graph_size()
            if state.get("full") and size and _grown(state.get("graph"), size) and now - float(state.get("t") or 0) >= REFULL_S:
                state["full"] = False                   # the graph changed a lot (the topology arrived after the start): all again
            if not state.get("full"):
                win = now - int(avail.keep_days) * 86400        # first collection: everything the slots still hold
            win = float(int(win // SLOT) * SLOT)
            items = self.compute(win - CONTEXT_S, now)
            items = [i for i in items if i["start"] >= win]
            rows = {i["id"]: (i["id"], i["start"], i["end"], i["kind"], i["root"]["id"], json.dumps(i, separators=(",", ":")))
                    for i in items}
            with self.store.transaction() as tx:
                # only what changed is written (B9): the window is computed again every round, most incidents in it are
                # the same as before
                old = {r[0]: tuple(r) for r in tx._db.execute('SELECT id, start, "end", kind, root, data FROM incident WHERE start >= ?',
                                                              (win,)).fetchall()}
                gone = [(k,) for k in old if k not in rows]
                if gone:
                    tx._db.executemany("DELETE FROM incident WHERE id=?", gone)
                changed = [r for k, r in rows.items() if old.get(k) != r]
                if changed:
                    tx._db.executemany('INSERT OR REPLACE INTO incident(id, start, "end", kind, root, data) VALUES(?,?,?,?,?,?)', changed)
                tx._db.execute("DELETE FROM incident WHERE start < ?", (now - KEEP_DAYS * 86400,))
            if not state.get("full"):
                self.store.set_setting(STATE, {"full": True, "t": now, "graph": size})
            self._at, self._ver = now, key
            self.version += 1
            return len(items)

    def _graph_size(self) -> list:
        fl = getattr(self.core, "failure", None)
        try:
            g = fl.graph() if fl is not None else None
        except Exception:
            g = None
        return [len(g.nodes), len(g.edges)] if g is not None else []

    def compute(self, since: float, now: float) -> list:
        """Incidents with a start after ``since`` from slots, journal and graph (nothing stored)."""
        avail = self.core.availability
        names = self._names()
        start_slot, cur = int(since // SLOT), int(now // SLOT)
        eps = []
        for subject, cells in avail.cells_all(start_slot, cur).items():
            info = names.get(subject)
            if info is None or info.get("hidden") or info.get("self"):
                continue
            if not any(cells.translate(_DOWN)):
                continue
            for ep in episodes(subject, cells, start_slot, now):
                ep["node"] = self.node_of(subject)
                eps.append(ep)
        j = self._journal()
        evs = j.between(since, now, ("dev_offline", "dev_back", "wan_down", "wan_up", "app_stop", "app_start")) if j else []
        self._refine(eps, evs)
        eps += self._wan(evs, since, now)
        fl = getattr(self.core, "failure", None)
        g = fl.graph() if fl is not None else None
        cache_d, cache_u = {}, {}

        def walk(node, direction, cache):
            if g is None or node not in g.nodes:
                return {}
            if node not in cache:
                found = fl.search(g, [node], direction)
                cache[node] = {k: (v[0], v[1], v[2], {"key": g.edges[v[3]][2], "cert": g.edges[v[3]][3], "vars": g.edges[v[3]][4]})
                               for k, v in found.items()}
            return cache[node]

        def can_root(node):
            info = names.get(self.subject_of(node))
            if info is None and g is not None and node in g.nodes:
                return g.nodes[node].get("dev_kind") not in NO_ROOT_KINDS
            return bool(info) and info.get("kind") not in NO_ROOT_KINDS or node == "internet"

        out = []
        for grp in group_by_time(eps):
            for b in bundle(grp, lambda n: walk(n, "down", cache_d), lambda n: walk(n, "up", cache_u), can_root):
                out.append(self._make(b, names, g, now))
        out += self._updates(evs, now)
        marks = self.marks()
        for inc in out:
            m = marks.get(inc["id"])
            if m:
                inc["mark"] = m
                if m.get("kind") == "maintenance":
                    inc["kind"] = "maintenance"
                    inc["why_kind"] = "marked"
        out.sort(key=lambda i: i["start"])
        return out

    def _refine(self, eps: list, evs: list) -> None:
        """Exact edges from the journal: a ``dev_offline`` close to the first bad slot, a ``dev_back`` close to the end."""
        by = {}
        for ev in evs:
            if ev["kind"] in ("dev_offline", "dev_back"):
                did = (ev.get("data") or {}).get("id")
                if did:
                    by.setdefault(did, []).append(ev)
        for ep in eps:
            lst = by.get(ep["node"])
            if not lst:
                continue
            for ev in lst:
                if ev["kind"] == "dev_offline" and ep["from"] - REFINE_BEFORE_S <= ev["t"] <= ep["from"] + REFINE_AFTER_S:
                    ep["from"] = float(ev["t"])
                    break
            if ep["to"] is not None:
                for ev in lst:
                    if ev["kind"] == "dev_back" and ep["to"] - REFINE_AFTER_S <= ev["t"] <= ep["to"] + REFINE_BEFORE_S and ev["t"] > ep["from"]:
                        ep["to"] = float(ev["t"])
                        break

    @staticmethod
    def _wan(evs: list, since: float, now: float) -> list:
        out, cur = [], None
        for ev in evs:
            if ev["kind"] == "wan_down" and cur is None:
                cur = {"subject": "internet", "node": "internet", "from": float(ev["t"]), "to": None, "open": True, "runs": 1}
            elif ev["kind"] == "wan_up" and cur is not None:
                cur["to"], cur["open"] = float(ev["t"]), False
                if out and cur["from"] - out[-1]["to"] <= FLAP_GAP_SLOTS * SLOT:
                    out[-1]["to"], out[-1]["runs"] = cur["to"], out[-1]["runs"] + 1
                else:
                    out.append(cur)
                cur = None
        if cur is not None:
            out.append(cur)
        return out

    def _updates(self, evs: list, now: float) -> list:
        """Lanloom's own updates: maintenance, never a fault."""
        out, stop = [], None
        for ev in evs:
            d = ev.get("data") or {}
            if ev["kind"] == "app_stop":
                stop = ev["t"]
            elif ev["kind"] == "app_start":
                prev, ver = d.get("prev"), d.get("version")
                if prev and ver and prev != ver:
                    start = float(stop if stop and ev["t"] - stop < 6 * 3600 else ev["t"] - float(d.get("down_s") or 60))
                    end = float(ev["t"])
                    iid = incident_id("lanloom", start)
                    out.append({"id": iid, "start": start, "end": end, "open": False, "dur": int(max(1, end - start)), "kind": "maintenance",
                                "why_kind": "update", "runs": 1,
                                "root": {"id": "lanloom", "kind": "lanloom", "title": "", "subject": "lanloom", "cert": "sure", "observed": True,
                                         "why": {"key": "update", "cert": "sure", "vars": {"from": str(prev)[:20], "to": str(ver)[:20]}}},
                                "members": [], "counts": {"members": 0, "sure": 0, "likely": 0}})
                stop = None
        return out

    def _make(self, b: dict, names: dict, g, now: float) -> dict:
        root = b["root"]
        subject = self.subject_of(root)
        info = names.get(subject) or {}
        gnode = g.nodes.get(root) if g is not None else None
        title = info.get("title") or (gnode or {}).get("title") or ""
        kind = info.get("kind") or (gnode or {}).get("dev_kind") or (gnode or {}).get("kind") or ("internet" if root == "internet" else "unknown")
        eps = b["eps"]
        own = eps[0] if b["observed"] else None
        start = own["from"] if own else min(e["from"] for e in eps)     # the root's own (refined) start when it was seen down
        is_open = any(e["open"] for e in eps)
        end = None if is_open else max(e["to"] for e in eps)
        runs = own["runs"] if own else max(e["runs"] for e in eps)
        dur = int(max(1, (now if is_open else end) - start))
        t_m = own["from"] if own else start
        maint = self._maint(t_m, root) if root not in ("internet",) else self._maint(t_m, None)
        k = kind_of(runs, dur, is_open, bool(maint))
        members = []
        for m in b["members"]:
            ep = m["ep"]
            if own and own["from"] - SLOT <= ep["from"] < own["from"]:
                m["ep"] = dict(ep, **{"from": own["from"]})      # the same slot as the root's exact time: not before it
        for m in sorted(b["members"], key=lambda m: (m["ep"]["from"], m["stage"] or 0))[:MAX_MEMBERS]:
            mi = names.get(self.subject_of(m["node"])) or {}
            gm = g.nodes.get(m["node"]) if g is not None else None
            members.append({"id": m["node"], "title": mi.get("title") or (gm or {}).get("title") or "", "kind": mi.get("kind") or (gm or {}).get("dev_kind") or "unknown",
                            "cert": m["cert"], "stage": m["stage"], "why": m["why"], "from": m["ep"]["from"], "to": m["ep"]["to"],
                            "open": m["ep"]["open"], "tiles": list((gm or {}).get("tiles") or [])})
        inc = {"id": incident_id(root, start), "start": start, "end": end, "open": is_open, "dur": dur, "kind": k, "runs": runs,
               "root": {"id": root, "kind": kind, "title": title, "ip": info.get("ip") or (gnode or {}).get("ip") or "", "subject": subject,
                        "cert": b["root_cert"], "observed": b["observed"], "why": b["root_why"],
                        "tiles": list((gnode or {}).get("tiles") or ([root] if root == "internet" else []))},
               "members": members,
               "counts": {"members": len(b["members"]), "sure": sum(1 for m in b["members"] if m["cert"] == "sure"),
                          "likely": sum(1 for m in b["members"] if m["cert"] != "sure")}}
        if own:
            inc["root"]["from"], inc["root"]["to"] = own["from"], own["to"]
        if maint:
            inc["why_kind"] = "window"
            inc["window"] = {"id": maint.get("id"), "name": str(maint.get("title") or maint.get("name") or "")[:60]}
        return inc

    # ------------------------------------------------------------------ reading
    def _rows(self, since: float, until: Optional[float] = None) -> list:
        until = self.clock() if until is None else until
        rows = self.store.query('SELECT data FROM incident WHERE start <= ? AND ("end" IS NULL OR "end" >= ?) ORDER BY start DESC LIMIT ?',
                                (until, since, MAX_LIST))
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["data"]))
            except ValueError:
                continue
        return out

    def list(self, days: float = 7, kind: Optional[str] = None, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        self.collect(now)
        days = max(1.0, min(float(CALENDAR_DAYS), float(days or 7)))
        items = self._rows(now - days * 86400, now)
        counts = {k: 0 for k in KINDS}
        for i in items:
            counts[i["kind"]] = counts.get(i["kind"], 0) + 1
        if kind in KINDS:
            items = [i for i in items if i["kind"] == kind]
        return {"t": now, "days": days, "items": [_short(i) for i in items], "counts": counts,
                "open": sum(1 for i in items if i["open"] and i["kind"] != "maintenance")}

    def get(self, iid: str, now: Optional[float] = None) -> Optional[dict]:
        now = self.clock() if now is None else now
        self.collect(now)
        row = self.store.one("SELECT data FROM incident WHERE id=?", (str(iid)[:40],))
        if not row:
            return None
        inc = json.loads(row["data"])
        inc["timeline"] = self._timeline(inc, now)
        inc["strip"] = self._strip(inc, now)
        inc["before"] = self.before(inc["start"], inc["root"]["id"])
        inc["heal"] = self._heal(inc, now)
        inc["chain_ok"] = inc["root"]["id"] not in ("lanloom",) and self._known(inc["root"]["id"])
        return inc

    def _known(self, node: str) -> bool:
        fl = getattr(self.core, "failure", None)
        try:
            return fl is not None and fl.resolve(fl.graph(), node) is not None
        except Exception:
            return False

    def _timeline(self, inc: dict, now: float) -> list:
        out = []
        r = inc["root"]
        if inc["kind"] == "maintenance" and inc.get("why_kind") == "update":
            v = r["why"]["vars"]
            out.append({"t": inc["start"], "key": "inc.tl.update_stop", "vars": {"from": v.get("from")}})
            out.append({"t": inc["end"], "key": "inc.tl.update_start", "vars": {"to": v.get("to")}})
            return out
        if r.get("observed", True):
            out.append({"t": r.get("from", inc["start"]), "key": "inc.tl.root_down", "vars": {"name": r["title"]}, "level": "danger"})
        else:
            out.append({"t": inc["start"], "key": "inc.tl.root_guess", "vars": {"name": r["title"], "n": inc["counts"]["members"]}, "level": "warn"})
        for m in inc["members"]:
            out.append({"t": m["from"], "key": "inc.tl.member_down", "vars": {"name": m["title"]}, "level": "warn", "cert": m["cert"]})
            if m["to"]:
                out.append({"t": m["to"], "key": "inc.tl.member_back", "vars": {"name": m["title"]}, "level": "ok"})
        if r.get("observed", True) and r.get("to"):
            out.append({"t": r["to"], "key": "inc.tl.root_back", "vars": {"name": r["title"]}, "level": "ok"})
        j = self._journal()
        if j is not None:
            until = inc["end"] or now
            ids = {r["id"]} | {m["id"] for m in inc["members"]}
            for ev in j.between(inc["start"] - 60, until + 60, ("src_fail", "src_ok", "wan_down", "wan_up")):
                d = ev.get("data") or {}
                if ev["kind"].startswith("wan") and r["id"] != "internet":
                    continue
                if ev["kind"].startswith("src") and d.get("id") not in ids:
                    continue
                out.append({"t": ev["t"], "key": "inc.tl." + ev["kind"], "vars": {"name": str(d.get("name") or "")[:60]},
                            "level": "ok" if ev["kind"] in ("src_ok", "wan_up") else "warn"})
        ids = {r["id"]} | {m["id"] for m in inc["members"]}
        try:
            rows = self.store.query("SELECT t, event, data, outcome FROM notify_log WHERE t >= ? AND t <= ? ORDER BY t LIMIT 200",
                                    (inc["start"] - 60, (inc["end"] or now) + 600))
        except Exception:
            rows = []
        for o in rows:
            try:
                d, oc = json.loads(o["data"] or "{}"), json.loads(o["outcome"] or "{}")
            except ValueError:
                continue
            if d.get("device") not in ids and not (r["id"] == "internet" and str(o["event"]).startswith("wan")):
                continue
            vals = sorted(set(oc.values())) if oc else ["nightly"]
            out.append({"t": o["t"], "key": "inc.tl.notified", "vars": {"event_key": "notify.ev." + str(o["event"]), "status_key": "notify.status." + str(vals[0])},
                        "level": "info"})
        if inc.get("window"):
            out.append({"t": inc["start"], "key": "inc.tl.window", "vars": {"name": inc["window"]["name"]}, "level": "info"})
        out.sort(key=lambda e: (e["t"] or 0))
        return _group_members(out)[:80]

    def _strip(self, inc: dict, now: float) -> dict:
        """Slots of the root (or the first member) from two hours before to two hours after the incident."""
        subj = inc["root"]["subject"] if inc["root"].get("observed", True) else (self.subject_of(inc["members"][0]["id"]) if inc["members"] else "")
        if not subj or subj in ("internet", "lanloom"):
            return {}
        a = int((inc["start"] - 7200) // SLOT)
        b = min(int(now // SLOT), int(((inc["end"] or now) + 7200) // SLOT))
        cells = self.core.availability.cells(subj, a, b)
        return {"subject": subj, "t0": a * SLOT, "step": SLOT, "slots": list(cells), "share": av.share_of(cells)}

    def _heal(self, inc: dict, now: float) -> dict:
        if inc["open"]:
            return {"key": "inc.heal.open", "vars": {"dur_dur": inc["dur"]}}
        if inc["kind"] == "maintenance":
            return {"key": "inc.heal.maintenance", "vars": {"dur_dur": inc["dur"]}}
        late = [m for m in inc["members"] if m["to"] and inc["root"].get("to") and m["to"] > inc["root"]["to"] + SLOT]
        if late:
            return {"key": "inc.heal.late", "vars": {"dur_dur": inc["dur"], "n": len(late)}}
        return {"key": "inc.heal.self", "vars": {"dur_dur": inc["dur"]}}

    # ------------------------------------------------------------------ "what changed before"
    def before(self, t: float, node: str = "") -> list:
        out = []
        j = self._journal()
        if j is not None:
            for ev in j.between(t - BEFORE_S, t, CHANGE_KINDS):
                d = ev.get("data") or {}
                if ev["kind"] == "app_start" and (not d.get("prev") or d.get("prev") == d.get("version")):
                    continue
                if ev["kind"] == "src_fail" and t - ev["t"] < 30:
                    continue
                vars_ = {"name": str(d.get("title") or d.get("name") or d.get("ip") or "")[:60]}
                if ev["kind"] == "app_start":
                    vars_ = {"from": str(d.get("prev"))[:20], "to": str(d.get("version"))[:20]}
                if ev["kind"] == "wan_switch":
                    vars_ = {"from": str(d.get("from") or "")[:40], "to": str(d.get("to") or "")[:40]}
                if ev["kind"] == "fw_change":
                    vars_ = {"n": int(d.get("n") or 1)}
                out.append({"t": ev["t"], "key": "inc.before." + ev["kind"], "vars": vars_, "id": d.get("id") or ""})
            out.extend(self._before_rules(j, t))
        ch = getattr(self.core, "chronicle", None)
        if ch is not None:
            for r in ch.between(t - BEFORE_FW_S, t, ("firmware",)):
                d = r["data"]
                out.append({"t": r["t"], "key": "inc.before.firmware", "vars": {"name": d.get("title") or "", "from": d.get("from") or "", "to": d.get("to") or ""},
                            "id": r["subject"], "near": r["subject"] == node})
        audit = getattr(self.core, "audit", None)
        if audit is not None:
            try:
                for e in audit.between(t - BEFORE_S, t):
                    act = str(e.get("action") or "")
                    if e.get("outcome") != "ok" or act.startswith(("auth.", "session.", "login", "user.", "token.", "setup.")):
                        continue
                    out.append({"t": e["t"], "key": "inc.before.audit", "vars": {"action": act[:60], "role_key": "role.name." + str(e.get("role") or "viewer")}})
            except Exception:
                pass
        out.sort(key=lambda e: -e["t"])
        return out[:20]

    def _before_rules(self, journal, t: float) -> list:
        """Rule changes of this site in the day before ``t`` (made on the device or in the controller, not by the editor):
        the most serious first, at most ``BEFORE_RULES_MAX``. A hint to check, not a cause."""
        from .firewall import drift
        rows = journal.between(t - BEFORE_FW_S, t, (drift.KIND,))
        rank = {name: i for i, name in enumerate(drift.LEVELS)}
        rows.sort(key=lambda r: (rank.get((r.get("data") or {}).get("level"), 2), -r["t"]))
        out = []
        multi = len({(r.get("data") or {}).get("integ") for r in rows}) > 1
        for r in rows[:BEFORE_RULES_MAX]:
            d = r.get("data") or {}
            desc = drift.describe(d)
            item = {"t": r["t"], "key": desc["key"], "vars": desc["vars"], "id": "", "level": d.get("level") if d.get("level") in drift.LEVELS else "info",
                    "check": True}
            if multi and d.get("controller"):
                item["where"] = str(d["controller"])[:40]
            out.append(item)
        return out

    def what_happened(self, ident: str, now: Optional[float] = None) -> Optional[dict]:
        """For one part: the incident it is in (open, else the latest of the last 24 hours), the probable root, since when,
        what changed before. None when the part is unknown."""
        now = self.clock() if now is None else now
        fl = getattr(self.core, "failure", None)
        node = str(ident or "")[:300]
        if fl is not None:
            try:
                g = fl.graph()
                node = fl.resolve(g, node) or node
            except Exception:
                pass
        self.collect(now)
        hits = []
        for inc in self._rows(now - 86400, now):
            ids = {inc["root"]["id"]} | {m["id"] for m in inc["members"]}
            if node in ids:
                hits.append(inc)
        if not hits:
            return {"node": node, "incident": None, "before": [], "calm": True}
        inc = sorted(hits, key=lambda i: (not i["open"], -i["start"]))[0]
        me = next((m for m in inc["members"] if m["id"] == node), None)
        return {"node": node, "incident": _short(inc), "is_root": me is None, "member": me, "since": inc["start"],
                "before": self.before(inc["start"], inc["root"]["id"]), "calm": False}

    # ------------------------------------------------------------------ overviews
    def calendar(self, now: Optional[float] = None, days: int = CALENDAR_DAYS, tz=None) -> dict:
        now = self.clock() if now is None else now
        self.collect(now)
        since = now - days * 86400
        per = {}
        for r in self.store.query('SELECT start, "end", kind FROM incident WHERE start >= ? ORDER BY start', (since,)):
            k = day_key(r["start"], tz)
            d = per.setdefault(k, {"n": 0, "kinds": {}, "down_s": 0})
            d["n"] += 1
            d["kinds"][r["kind"]] = d["kinds"].get(r["kind"], 0) + 1
            if r["kind"] != "maintenance":
                d["down_s"] += int(((r["end"] or now) - r["start"]))
        out, worst = [], 0
        for i in range(days - 1, -1, -1):
            k = day_key(now - i * 86400, tz)
            d = per.get(k, {"n": 0, "kinds": {}, "down_s": 0})
            faults = d["n"] - d["kinds"].get("maintenance", 0)
            worst = max(worst, faults)
            out.append({"day": k, "n": d["n"], "faults": faults, "kinds": d["kinds"], "down_s": d["down_s"]})
        first = self.store.one("SELECT MIN(start) AS s FROM incident")
        return {"days": out, "max": worst, "since": first["s"] if first else None}

    def heatmap(self, now: Optional[float] = None, days: int = 7, tz=None) -> dict:
        """Hour of day x day: how many parts went down (incident starts and members), the last ``days`` days."""
        now = self.clock() if now is None else now
        self.collect(now)
        rows = self._rows(now - days * 86400, now)
        keys = [day_key(now - i * 86400, tz) for i in range(days - 1, -1, -1)]
        grid = {k: [0] * 24 for k in keys}
        hour = (lambda ts: time.localtime(ts).tm_hour) if tz is None else (lambda ts: tz(ts).tm_hour)
        for inc in rows:
            if inc["kind"] == "maintenance":
                continue
            for ts in [inc["start"]] + [m["from"] for m in inc["members"]]:
                k = day_key(ts, tz)
                if k in grid:
                    grid[k][hour(ts)] += 1
        j = self._journal()
        events = {k: [0] * 24 for k in keys}
        if j is not None:
            for ev in j.between(now - days * 86400, now):
                k = day_key(ev["t"], tz)
                if k in events:
                    events[k][hour(ev["t"])] += 1
        return {"days": keys, "down": [grid[k] for k in keys], "events": [events[k] for k in keys],
                "max": max([max(r) for r in grid.values()] + [0]), "max_events": max([max(r) for r in events.values()] + [0])}

    def events(self, hours: float = 24, now: Optional[float] = None, limit: int = 300) -> dict:
        """The event chronicle: journal rows of the last hours, newest first, with the incident they belong to."""
        now = self.clock() if now is None else now
        hours = max(1.0, min(24 * 7.0, float(hours or 24)))
        j = self._journal()
        rows = j.between(now - hours * 3600, now) if j is not None else []
        incs = self._rows(now - hours * 3600, now)
        out = []
        for ev in reversed(rows):
            d = ev.get("data") or {}
            if ev["kind"] in ("app_stop",):
                continue
            item = {"t": ev["t"], "kind": ev["kind"], "name": str(d.get("title") or d.get("name") or d.get("ip") or "")[:60], "id": d.get("id") or ""}
            if ev["kind"] == "app_start":
                item["vars"] = {"version": str(d.get("version") or "")[:20], "prev": str(d.get("prev") or "")[:20]}
            for inc in incs:
                if inc["start"] - 60 <= ev["t"] <= (inc["end"] or now) + 60 and (item["id"] in ({inc["root"]["id"]} | {m["id"] for m in inc["members"]})):
                    item["incident"] = inc["id"]
                    break
            out.append(item)
            if len(out) >= limit:
                break
        return {"t": now, "hours": hours, "items": out}

    def night(self, since: float, until: float) -> list:
        self.collect(until)
        return [i for i in self._rows(since, until) if i["start"] < until]

    def open_faults(self, now: Optional[float] = None) -> list:
        now = self.clock() if now is None else now
        self.collect(now)
        return [_short(i) for i in self._rows(now - 3600, now) if i["open"] and i["kind"] != "maintenance"]

    def overview(self, now: Optional[float] = None) -> dict:
        """Key figure of the start page: open faults, faults of the last 7 days, the newest one."""
        now = self.clock() if now is None else now
        self.collect(now)
        rows = [i for i in self._rows(now - 7 * 86400, now) if i["kind"] != "maintenance"]
        opened = [i for i in rows if i["open"]]
        return {"open": len(opened), "week": len(rows), "latest": _short(rows[0]) if rows else None,
                "open_items": [_short(i) for i in opened[:4]]}

    def marks_for_range(self, since: float, until: float) -> list:
        """Marks for the rewind time line: [{id, kind, start, end, title, root}]."""
        self.collect(until)
        return [{"id": i["id"], "kind": i["kind"], "start": i["start"], "end": i["end"], "title": i["root"]["title"], "root": i["root"]["id"],
                 "root_kind": i["root"]["kind"], "members": len(i["members"])} for i in self._rows(since, until)]

    # ------------------------------------------------------------------ the user's mark
    def mark(self, iid: str, kind: Optional[str], note: str = "", now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        iid = str(iid or "")[:40]
        if not self.store.one("SELECT id FROM incident WHERE id=?", (iid,)):
            raise KeyError("inc.err.unknown")
        if kind not in (None, "maintenance"):
            raise ValueError("inc.err.invalid")
        note = str(note or "").strip()[:NOTE_MAX]
        marks = self.marks()
        if kind is None and not note:
            marks.pop(iid, None)
        else:
            marks[iid] = {"kind": kind, "note": note, "t": now}
        if len(marks) > 2000:
            for k in sorted(marks, key=lambda k: marks[k].get("t", 0))[:len(marks) - 2000]:
                marks.pop(k, None)
        self.store.set_setting(MARK_SETTING, marks)
        row = self.store.one("SELECT start FROM incident WHERE id=?", (iid,))
        # the mark applies at once: the row is computed again (older rows get the mark written into them)
        if row and row["start"] < now - WINDOW_S:
            data = json.loads(self.store.one("SELECT data FROM incident WHERE id=?", (iid,))["data"])
            data["mark"] = marks.get(iid)
            if kind == "maintenance":
                data["kind"], data["why_kind"] = "maintenance", "marked"
            elif data.get("why_kind") == "marked":
                data["kind"] = kind_of(data.get("runs", 1), data["dur"], data["open"], False)
                data.pop("why_kind", None)
            self.store.execute("UPDATE incident SET data=?, kind=? WHERE id=?", (json.dumps(data, separators=(",", ":")), data["kind"], iid))
        else:
            self.collect(now, force=True)
        return {"id": iid, "mark": marks.get(iid)}


def _group_members(rows: list, gap: float = 120.0) -> list:
    """Members that went down (or came back) within two minutes of each other become one line with their names."""
    out = []
    for e in rows:
        k = e["key"]
        if k in ("inc.tl.member_down", "inc.tl.member_back") and out and out[-1].get("_k") == k and e["t"] - out[-1]["_t0"] <= gap:
            last = out[-1]
            last["_names"].append(e["vars"]["name"])
            if e.get("cert") == "likely":
                last["_likely"] += 1
            continue
        if k in ("inc.tl.member_down", "inc.tl.member_back"):
            out.append(dict(e, _k=k, _t0=e["t"], _names=[e["vars"]["name"]], _likely=1 if e.get("cert") == "likely" else 0))
        else:
            out.append(e)
    for e in out:
        if "_k" not in e:
            continue
        names = e.pop("_names")
        k, likely = e.pop("_k"), e.pop("_likely")
        e.pop("_t0", None)
        if len(names) > 1:
            shown = names[:4]
            more = len(names) - len(shown)
            e["key"] = ("inc.tl.members_down" if k.endswith("down") else "inc.tl.members_back") + ("_more" if more else "")
            e["vars"] = {"n": len(names), "names": ", ".join(shown), "more": more}
            e.pop("cert", None)
            if likely:
                e["cert"] = "likely" if likely == len(names) else "mixed"
    return out


def _short(i: dict) -> dict:
    """The list form of an incident (no time line, no slots)."""
    keep = ("id", "start", "end", "open", "dur", "kind", "runs", "counts", "why_kind", "mark")
    out = {k: i[k] for k in keep if k in i}
    r = i["root"]
    out["root"] = {k: r[k] for k in ("id", "kind", "title", "ip", "cert", "observed", "why", "tiles") if k in r}
    out["members"] = [{"id": m["id"], "title": m["title"], "kind": m["kind"], "cert": m["cert"], "tiles": m.get("tiles") or []} for m in i["members"][:12]]
    if i.get("window"):
        out["window"] = i["window"]
    return out


def _note(core, now: float) -> list:
    """Noteworthy list of the start page: an open incident that took other parts with it (a single device that is down is
    already a line of its own there)."""
    inc = getattr(core, "incidents", None)
    if inc is None:
        return []
    out = []
    for i in inc.open_faults(now):
        n = i["counts"]["members"]
        if not n:
            continue
        guess = not i["root"].get("observed", True)
        out.append({"id": "inc:" + i["id"], "level": "danger" if i["root"].get("kind") in ("router", "switch") else "warn",
                    "key": "ov.note.incident_guess" if guess else "ov.note.incident_open", "sub": "ov.note.incident_open.sub",
                    "vars": {"name": i["root"]["title"] or i["root"]["id"], "n": n, "age_dur": int(now - i["start"])},
                    "link": "#/incidents/" + i["id"]})
    return out


def _register_note() -> None:
    from . import overview
    overview.register_note(_note)


_register_note()
