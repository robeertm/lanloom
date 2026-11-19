"""Failure chain: "What fails if X fails?" and "What does X depend on?" - only from what Lanloom already knows, nothing
is guessed and nothing is switched.

One typed dependency graph over the network and the hub. An edge (a, b) always means **a depends on b** (when b fails,
a is affected). Every edge carries a reason (``why``, an i18n key below ``fail.why.``) and a certainty:

* ``sure``   - when b fails, a fails with it (hangs on its port, runs on it, is started by it).
* ``likely`` - when b fails, a is impaired (a part of its job is missing, or the way is not known for certain).

Edges, from the sources the core already holds:

  port / radio / virtual   resolved topology (lanloom/topology.py): a device hangs on its parent's port, in its radio or runs as
                           a guest on its host. sure.
  gateway                  a device without a known port reaches the rest only behind the gateway. sure (the gateway, or a
                           switch built into it, is the only way that is known).
  internet                 the internet (as seen from the LAN) needs the gateway. sure.
  container                a container runs on its host. sure.
  connection               a connection of Lanloom (hub, broker, counters, controller ...) needs the device at its address. sure.
  hub / via / same         a hub device needs the hub (or the radio coordinator it is reached through, ``via``); a hub device
                           that is one component with a network device (lanloom/hubmerge.py) needs that device too. sure.
  mqtt                     a hub device of the message-broker integration needs the broker connection, when exactly one is
                           set up. likely (the hub may use another broker).
  entity / entity_hub      an entity belongs to its hub device (or, without one, to the hub). sure.
  auto_hub                 an automation or script runs in the hub. sure.
  trigger                  an automation needs what starts it. sure (it never starts without it).
  condition / reads / template / calls
                           an automation reads or calls this while it runs. likely.
  switched / acts          an entity an automation switches is no longer switched automatically when the automation fails,
                           and the automation cannot do its job when that entity fails. likely (both directions).
  manual                   the user's own entries on the component card: ``hard`` (sure), ``soft`` (likely) or ``none``
                           (there is no dependency). A manual entry decides the pair: the automatic edges between the two
                           (both directions) are dropped.

Spreading (breadth first, the stage is the distance):
  * over a sure edge the neighbour counts as **failed** and the search goes on from it ("sure" continues);
  * over a likely edge it counts as **impaired** - it is listed, but the search ends there ("likely" ends). Otherwise
    one partial failure would colour the whole home.
  * Depth is limited (``DEPTH``, at most ``DEPTH_MAX``); every node is visited once, so loops are safe.

Outage automatic (``outages``): every device that is down right now and was seen up before (the same rule as the
"Notable" list of the overview) is a root; phones and computers come and go and never start it. The union of their
chains marks the likely affected parts on the map (dashed), the card says why ("probably affected: X is down"), the
Automations tab marks affected automations.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

from . import hub_privacy as privacy

SURE, LIKELY = "sure", "likely"
DEPTH = 8
DEPTH_MAX = 12
DEPTH_OUTAGE = 8
KINDS = ("device", "node", "container", "connection", "hub", "hub_device", "automation", "script", "entity", "internet")
KIND_RANK = {k: i for i, k in enumerate(KINDS)}
CAP = {"device": 300, "node": 100, "container": 120, "connection": 60, "hub": 20, "hub_device": 300, "automation": 200,
       "script": 100, "entity": 200, "internet": 1}
MANUAL_KINDS = ("hard", "soft", "none")
MANUAL_CERT = {"hard": SURE, "soft": LIKELY}
MAX_MANUAL_PER_NODE = 40
MAX_MANUAL = 2000
NOTE_MAX = 120
NO_ROOT_KINDS = ("phone", "computer")       # they leave and sleep: never a root of the outage automatic
SPECIAL_TILE = {"hub": "hub:%d", "mqtt": "broker:%d", "snmp": "snmp:%d"}
SETTING = "failure_deps"
GRAPH_TTL_S = 2.0          # the key (structure, topology, hub, containers, manual entries) is checked at most this often


def _host_of(value: str) -> str:
    value = str(value or "")
    if "://" in value:
        return urlparse(value).hostname or ""
    return value


class Graph:
    """Node infos, the edge list and neighbour lists in both directions."""

    def __init__(self):
        self.nodes: dict = {}          # id -> {kind, title, ...}
        self.edges: list = []          # (a, b, why, cert, vars)   a depends on b
        self.up: dict = {}             # a -> [edge index]  (what a depends on)
        self.down: dict = {}           # b -> [edge index]  (who depends on b)
        self.ms = 0.0
        self.stale: list = []          # manual entries whose node or target is gone

    def node(self, nid: str, **info) -> dict:
        if nid not in self.nodes:
            self.nodes[nid] = info
        return self.nodes[nid]

    def edge(self, a: str, b: str, why: str, cert: str, **vars_) -> None:
        if a == b or a not in self.nodes or b not in self.nodes:
            return
        self.edges.append((a, b, why, cert, vars_))

    def index(self) -> None:
        seen, keep = set(), []
        for e in self.edges:
            key = (e[0], e[1])
            if key in seen:            # one edge per pair: the first (strongest source) wins
                continue
            seen.add(key)
            keep.append(e)
        self.edges = keep
        self.up, self.down = {}, {}
        for i, (a, b, _w, _c, _v) in enumerate(keep):
            self.up.setdefault(a, []).append(i)
            self.down.setdefault(b, []).append(i)


# ---------------------------------------------------------------------- manual entries (stored in the settings)
def manual_entries(store) -> dict:
    raw = store.get_setting(SETTING, {}) or {}
    return raw if isinstance(raw, dict) else {}


def set_manual(store, node: str, target: str, kind: str | None, note: str = "", now: float | None = None) -> dict:
    """kind hard|soft|none, or None/"" to remove the entry. Raises ValueError(i18n key)."""
    entries = manual_entries(store)
    mine = dict(entries.get(node) or {})
    if not kind:
        mine.pop(target, None)
    else:
        if kind not in MANUAL_KINDS:
            raise ValueError("fail.err.kind")
        clean = "".join(ch for ch in str(note or "") if ch.isprintable()).strip()[:NOTE_MAX]
        if target not in mine and len(mine) >= MAX_MANUAL_PER_NODE:
            raise ValueError("fail.err.too_many")
        if target not in mine and sum(len(v) for v in entries.values()) >= MAX_MANUAL:
            raise ValueError("fail.err.too_many")
        mine[target] = {"k": kind, "note": clean, "t": round(time.time() if now is None else now)}
    if mine:
        entries[node] = mine
    else:
        entries.pop(node, None)
    store.set_setting(SETTING, entries)
    return entries


# ---------------------------------------------------------------------- the graph
class Failure:
    def __init__(self, core):
        self.core = core
        self.lock = threading.Lock()
        self._g = None                 # (key, t, Graph)
        self._outage = None            # (key, result)

    # --------------------------------------------------------------- building
    def _key(self) -> tuple:
        core = self.core
        hub = tuple(sorted((i, (e.get("snapshot") or {}).get("t"), (e.get("deps") or {}).get("t")) for i, e in core.hub.items()))
        agents = getattr(core, "agents", None)
        ct = tuple(sorted((dev, len(items)) for dev, items in agents.containers_by_device().items())) if agents is not None else ()
        topo = core.topology()
        return (core.structure_version, topo.get("signature"), hub, ct, repr(manual_entries(core.store)))

    def graph(self) -> Graph:
        now = time.monotonic()
        with self.lock:
            hit = self._g
            if hit and now - hit[1] < GRAPH_TTL_S:
                return hit[2]
            key = self._key()
            if hit and hit[0] == key:
                self._g = (key, now, hit[2])
                return hit[2]
            t0 = time.perf_counter()
            g = self._build()
            g.ms = round((time.perf_counter() - t0) * 1000, 1)
            self._g = (key, now, g)
            self._outage = None
            return g

    def invalidate(self) -> None:
        with self.lock:
            self._g = None
            self._outage = None

    def _build(self) -> Graph:
        core = self.core
        g = Graph()
        devices = core.devices()
        alias = {d["id"]: d["merged_into"]["id"] for d in devices if isinstance(d.get("merged_into"), dict) and d["merged_into"].get("id")}
        real = [d for d in devices if not d.get("merged_into")]
        by_ip = {}
        gw = None
        for d in real:
            g.node(d["id"], kind="device", title=d.get("title") or "", dev_kind=d.get("kind") or "unknown", ip=d.get("ip") or "",
                   hidden=bool(d.get("hidden")), is_self=bool(d.get("is_self")),
                   gateway=bool(d.get("is_gateway")), tiles=[d["id"]])
            if d.get("ip"):
                by_ip.setdefault(d["ip"], d["id"])
            if d.get("is_gateway") and gw is None:
                gw = d["id"]
        topo = core.topology()
        for vid, n in (topo.get("nodes") or {}).items():
            g.node(vid, kind="node", title=n.get("title") or "", dev_kind=n.get("kind") or "switch", ip=n.get("ip") or "", tiles=[vid])
        if gw:
            g.node("internet", kind="internet", title="", tiles=["internet"])
            g.edge("internet", gw, "internet", SURE)
        links = topo.get("links") or {}
        for child, l in links.items():
            a, b = alias.get(child, child), alias.get(l["parent"], l["parent"])
            why = {"radio": "radio", "virtual": "virtual"}.get(l.get("medium"), "port")
            g.edge(a, b, why, SURE, port=l.get("port") or "")
        if gw:
            for nid, info in g.nodes.items():
                if info["kind"] in ("device", "node") and nid != gw and nid not in links and not (info["kind"] == "device" and alias.get(nid)):
                    g.edge(nid, gw, "gateway", SURE)
        # containers on their hosts
        agents = getattr(core, "agents", None)
        if agents is not None:
            for host, items in agents.containers_by_device().items():
                host = alias.get(host, host)
                for aid, c in items:
                    cid = "ct:%s:%s" % (aid, c.get("name"))
                    g.node(cid, kind="container", title=str(c.get("name") or ""), tiles=[cid])
                    g.edge(cid, host, "container", SURE)
        # connections of Lanloom
        integs = core.integrations()
        mqtt = [i["id"] for i in integs if i["kind"] == "mqtt"]
        for integ in integs:
            iid = integ["id"]
            host = _host_of(integ["config"].get("url") or integ["config"].get("host") or "")
            dev = alias.get(by_ip.get(host), by_ip.get(host))
            tiles = [dev] if dev else ([SPECIAL_TILE[integ["kind"]] % iid] if integ["kind"] in SPECIAL_TILE else [])
            g.node("int:%d" % iid, kind="hub" if integ["kind"] == "hub" else "connection", title=integ["name"], integ_kind=integ["kind"],
                   tiles=tiles)
            if dev:
                g.edge("int:%d" % iid, dev, "connection", SURE)
        # the hub: devices, entities, automations and what they reference
        merge = core.hub_merge() if core.hub else {"by_hub": {}}
        for iid, entry in sorted(core.hub.items()):
            snap = entry.get("snapshot") or {}
            if not snap:
                continue
            self._hub(g, iid, entry, snap, merge.get("by_hub") or {}, alias, mqtt[0] if len(mqtt) == 1 else None)
        self._manual(g)
        g.index()
        return g

    def _hub(self, g: Graph, iid: int, entry: dict, snap: dict, by_hub: dict, alias: dict, mqtt) -> None:
        hub = "int:%d" % iid
        if hub not in g.nodes:
            return
        hub_tiles = g.nodes[hub]["tiles"]
        devs = {d["id"]: d for d in snap.get("devices") or [] if isinstance(d, dict) and d.get("id")}
        dev_tiles = {}
        for did, d in devs.items():
            net = by_hub.get("%d:%s" % (iid, did))
            net = alias.get(net, net)
            tiles = [net] if net else ["rd:%d:%s" % (iid, did)]
            if d.get("area"):
                tiles.append("area:%d:%s" % (iid, d["area"]))
            dev_tiles[did] = tiles
            g.node("hd:%d:%s" % (iid, did), kind="hub_device", title=d.get("name") or did, integ=iid, tiles=tiles, host_tiles=hub_tiles,
                   side="hub" if net else None)
        for did, d in devs.items():
            nid = "hd:%d:%s" % (iid, did)
            via = d.get("via")
            if via and via in devs and via != did:
                g.edge(nid, "hd:%d:%s" % (iid, via), "via", SURE)
            else:
                g.edge(nid, hub, "hub", SURE)
            net = by_hub.get("%d:%s" % (iid, did))
            if net:
                g.edge(nid, alias.get(net, net), "same", SURE)
            if mqtt is not None and "mqtt" in (d.get("domains") or []):
                g.edge(nid, "int:%d" % mqtt, "mqtt", LIKELY)
        items = list(snap.get("entities") or [])
        known = {e.get("entity_id") for e in items if isinstance(e, dict)}
        for extra in (snap.get("automations") or []) + (snap.get("scripts") or []):
            if isinstance(extra, dict) and extra.get("entity_id") not in known:
                items.append({"entity_id": extra.get("entity_id"), "name": extra.get("name") or extra.get("alias")})
        for e in items:
            if not isinstance(e, dict):
                continue
            eid = e.get("entity_id")
            if not privacy.valid_entity_id(eid) or privacy.is_private(eid):
                continue
            dom = privacy.domain_of(eid)
            nid = "he:%d:%s" % (iid, eid)
            did = e.get("device_id")
            if dom in ("automation", "script"):
                g.node(nid, kind=dom, title=e.get("name") or eid, entity_id=eid, integ=iid, tiles=[], host_tiles=hub_tiles)
                g.edge(nid, hub, "auto_hub", SURE)
                continue
            tiles = list(dev_tiles.get(did) or [])
            if e.get("area") and "area:%d:%s" % (iid, e["area"]) not in tiles:
                tiles.append("area:%d:%s" % (iid, e["area"]))
            side = (g.nodes.get("hd:%d:%s" % (iid, did)) or {}).get("side") if did else None
            g.node(nid, kind="entity", title=e.get("name") or eid, entity_id=eid, integ=iid, tiles=tiles, host_tiles=hub_tiles, side=side)
            if did and did in devs:
                g.edge(nid, "hd:%d:%s" % (iid, did), "entity", SURE)
            else:
                g.edge(nid, hub, "entity_hub", SURE)
        deps = entry.get("deps") or {}
        refs = dict(deps.get("automations") or {}, **(deps.get("scripts") or {}))
        from . import hubauto
        for item_id, r in sorted(refs.items()):
            if not isinstance(r, dict):
                continue
            a = "he:%d:%s" % (iid, item_id)
            if a not in g.nodes:
                continue
            ent = lambda x: "he:%d:%s" % (iid, x)          # noqa: E731 - small local helper
            for x in r.get("trigger") or ():
                g.edge(a, ent(x), "trigger", SURE)
            for x in r.get("condition") or ():
                g.edge(a, ent(x), "condition", LIKELY)
            flow = r.get("flow") if isinstance(r.get("flow"), dict) else None
            targets = hubauto._target_ids(flow) if flow else None
            for x in r.get("action") or ():
                dom = privacy.domain_of(x)
                if dom in ("script", "automation"):
                    g.edge(a, ent(x), "calls", LIKELY)
                elif targets is None or x in targets:
                    g.edge(ent(x), a, "switched", LIKELY)
                    g.edge(a, ent(x), "acts", LIKELY)
                else:
                    g.edge(a, ent(x), "reads", LIKELY)
            for x in r.get("text") or ():
                g.edge(a, ent(x), "template", LIKELY)

    def _manual(self, g: Graph) -> None:
        entries = manual_entries(self.core.store)
        drop = set()
        add = []
        for node, targets in entries.items():
            if not isinstance(targets, dict):
                continue
            for target, m in targets.items():
                if not isinstance(m, dict) or m.get("k") not in MANUAL_KINDS:
                    continue
                if node not in g.nodes or target not in g.nodes:
                    g.stale.append({"node": node, "target": target, "k": m["k"], "missing": target if node in g.nodes else node})
                    continue
                drop.add(frozenset((node, target)))
                if m["k"] in MANUAL_CERT:
                    add.append((node, target, m))
        if drop:
            g.edges = [e for e in g.edges if frozenset((e[0], e[1])) not in drop]
        for node, target, m in add:
            g.edges.insert(0, (node, target, "manual_" + m["k"], MANUAL_CERT[m["k"]], {"note": m.get("note") or ""}))

    # --------------------------------------------------------------- reading
    def resolve(self, g: Graph, ident: str):
        """An id of the map, the palette or the API -> node id, or None."""
        ident = str(ident or "").strip()
        if not ident or len(ident) > 300:
            return None
        if ident in g.nodes:
            return ident
        head, _, rest = ident.partition(":")
        if head == "rd" and ("hd:" + rest) in g.nodes:
            return "hd:" + rest
        if head in ("hub", "broker", "snmp", "dns") and rest.isdigit() and ("int:" + rest) in g.nodes:
            return "int:" + rest
        if head == "ent" and ("he:" + rest) in g.nodes:
            return "he:" + rest
        return None

    @staticmethod
    def search(g: Graph, roots, direction: str = "down", depth: int = DEPTH) -> dict:
        """Breadth first from the roots. -> {node: (stage, certainty, previous, edge index)} without the roots."""
        nb = g.down if direction == "down" else g.up
        other = 0 if direction == "down" else 1          # in g.edges[i]: down -> the dependant is in front (a), up -> the dependency (b)
        depth = max(1, min(DEPTH_MAX, int(depth or DEPTH)))
        out, rootset = {}, set(roots)
        row, stage = list(rootset), 0
        while row and stage < depth:
            stage += 1
            nxt = []
            for u in row:
                for i in nb.get(u, ()):
                    e = g.edges[i]
                    v = e[other]
                    if v in rootset:
                        continue
                    old = out.get(v)
                    if e[3] == SURE:
                        if old is None or old[1] != SURE:
                            out[v] = (stage, SURE, u, i)
                            nxt.append(v)
                    elif old is None:
                        out[v] = (stage, LIKELY, u, i)
            row = nxt
        return out

    def view(self, g: Graph, nid: str) -> dict:
        info = g.nodes[nid]
        own = list(info.get("tiles") or [])
        d = {"id": nid, "kind": info["kind"], "title": info.get("title") or "", "tiles": own + list(info.get("host_tiles") or [])}
        if info.get("host_tiles"):
            d["own"] = len(own)          # the tiles after these are only the hub the part lives in (a chain lights it, an outage does not)
        for f in ("dev_kind", "ip", "entity_id", "integ_kind"):
            if info.get(f):
                d[f] = info[f]
        if info.get("is_self"):
            d["self"] = True
        if info.get("side"):
            d["side"] = info["side"]     # the hub side of a component the map draws as one tile with its network device
        if info.get("gateway"):
            d["gateway"] = True
        return d

    def name(self, g: Graph, nid: str) -> str:
        info = g.nodes.get(nid) or {}
        return info.get("title") or info.get("ip") or info.get("entity_id") or nid

    def _entry(self, g: Graph, nid: str, rec: tuple, found: dict, roots: set) -> dict:
        path, x = [nid], nid
        for _ in range(DEPTH_MAX + 2):
            r = found.get(x)
            if not r:
                break
            x = r[2]
            path.append(x)
            if x in roots:
                break
        path.reverse()
        e = g.edges[rec[3]]
        d = self.view(g, nid)
        d.update({"stage": rec[0], "cert": rec[1], "path": path, "path_names": [self.name(g, p) for p in path],
                  "why": {"key": e[2], "cert": e[3], "vars": e[4]}})
        return d

    @staticmethod
    def counts(g: Graph, found: dict) -> tuple:
        per = {k: 0 for k in KINDS}
        sure, stages = 0, {}
        for nid, rec in found.items():
            k = g.nodes[nid]["kind"]
            per[k] = per.get(k, 0) + 1
            sure += rec[1] == SURE
            stages[rec[0]] = stages.get(rec[0], 0) + 1
        return ({"total": len(found), "sure": sure, "likely": len(found) - sure,
                 "devices": per["device"] + per["node"] + per["container"], "hub_devices": per["hub_device"],
                 "automations": per["automation"] + per["script"], "entities": per["entity"],
                 "connections": per["connection"] + per["hub"], "per_kind": per},
                [{"stage": s, "n": n} for s, n in sorted(stages.items())])

    def _list(self, g: Graph, found: dict, roots: set) -> tuple:
        order = sorted(found.items(), key=lambda kv: (kv[1][0], KIND_RANK.get(g.nodes[kv[0]]["kind"], 99), kv[1][1] != SURE,
                                                     self.name(g, kv[0]).casefold()))
        n_kind, out, more = {}, [], {}
        for nid, rec in order:
            k = g.nodes[nid]["kind"]
            n_kind[k] = n_kind.get(k, 0) + 1
            if n_kind[k] > CAP.get(k, 100):
                more[k] = more.get(k, 0) + 1
                continue
            out.append(self._entry(g, nid, rec, found, roots))
        return out, more

    def chain(self, ident: str, direction: str = "down", depth: int = DEPTH):
        """The failure chain of one node (``down``: what fails if it fails; ``up``: what it depends on). None when unknown."""
        t0 = time.perf_counter()
        direction = "up" if direction == "up" else "down"
        g = self.graph()
        nid = self.resolve(g, ident)
        if nid is None:
            return None
        depth = max(1, min(DEPTH_MAX, int(depth or DEPTH)))
        found = self.search(g, [nid], direction, depth)
        counts, stages = self.counts(g, found)
        items, more = self._list(g, found, {nid})
        return {"root": self.view(g, nid), "dir": direction, "depth": depth, "counts": counts, "stages": stages, "items": items,
                "more": more, "graph": {"nodes": len(g.nodes), "edges": len(g.edges), "ms_build": g.ms},
                "ms": round((time.perf_counter() - t0) * 1000, 1)}

    # --------------------------------------------------------------- the outage automatic
    def roots(self, g: Graph) -> list:
        core = self.core
        watch = getattr(getattr(core, "reports", None), "watch", None)
        seen = watch.dev if watch is not None else {}
        status = {d["id"]: d.get("status") for d in core.devices()}      # always fresh: statuses never bump the graph
        out = []
        for nid, info in g.nodes.items():
            if info["kind"] != "device" or status.get(nid) != "down" or info.get("hidden"):
                continue
            if info.get("dev_kind") in NO_ROOT_KINDS or seen.get(nid) not in ("up", "down", "gone"):
                continue
            out.append(nid)
        return sorted(out)

    def outages(self, depth: int = DEPTH_OUTAGE) -> dict:
        t0 = time.perf_counter()
        g = self.graph()
        roots = self.roots(g)
        key = (id(g), tuple(roots), depth)
        with self.lock:
            hit = self._outage
        if hit and hit[0] == key:
            return dict(hit[1], ms=round((time.perf_counter() - t0) * 1000, 1))
        rootset = set(roots)
        joined = {}
        per_root = {}
        for r in roots:
            found = self.search(g, [r], "down", depth)
            per_root[r] = self.counts(g, found)[0]
            for nid, rec in found.items():
                if nid in rootset:
                    continue
                v = joined.get(nid)
                if v is None:
                    joined[nid] = {"rec": rec, "found": found, "root": r, "causes": [r]}
                else:
                    v["causes"].append(r)
                    if (rec[1] == SURE and v["rec"][1] != SURE) or (rec[1] == v["rec"][1] and rec[0] < v["rec"][0]):
                        v.update(rec=rec, found=found, root=r)
        order = sorted(joined.items(), key=lambda kv: (kv[1]["rec"][0], KIND_RANK.get(g.nodes[kv[0]]["kind"], 99),
                                                      self.name(g, kv[0]).casefold()))
        n_kind, items, more = {}, [], {}
        for nid, v in order:
            k = g.nodes[nid]["kind"]
            n_kind[k] = n_kind.get(k, 0) + 1
            if n_kind[k] > CAP.get(k, 100):
                more[k] = more.get(k, 0) + 1
                continue
            d = self._entry(g, nid, v["rec"], v["found"], {v["root"]})
            d["causes"] = v["causes"]
            items.append(d)
        counts, stages = self.counts(g, {n: v["rec"] for n, v in joined.items()})
        watch = getattr(getattr(self.core, "reports", None), "watch", None)
        since = watch.down_since if watch is not None else {}
        outs = []
        for r in roots:
            d = self.view(g, r)
            d["since"] = since.get(r)
            d["counts"] = per_root[r]
            outs.append(d)
        res = {"outages": outs, "items": items, "more": more, "counts": counts, "stages": stages, "depth": depth}
        with self.lock:
            self._outage = (key, res)
        return dict(res, ms=round((time.perf_counter() - t0) * 1000, 1))

    # --------------------------------------------------------------- the card: what this depends on, manual entries
    def deps(self, ident: str):
        g = self.graph()
        nid = self.resolve(g, ident)
        if nid is None:
            return None
        manual = []
        for target, m in sorted((manual_entries(self.core.store).get(nid) or {}).items()):
            if not isinstance(m, dict):
                continue
            row = {"target": target, "k": m.get("k"), "note": m.get("note") or "", "t": m.get("t"), "missing": target not in g.nodes}
            if target in g.nodes:
                row["node"] = self.view(g, target)
            manual.append(row)
        up = []
        for i in g.up.get(nid, ()):
            a, b, why, cert, vars_ = g.edges[i]
            up.append(dict(self.view(g, b), why={"key": why, "cert": cert, "vars": vars_}))
        up.sort(key=lambda r: (r["why"]["cert"] != SURE, KIND_RANK.get(r["kind"], 99), (r["title"] or r["id"]).casefold()))
        return {"node": self.view(g, nid), "manual": manual, "up": up[:60], "up_more": max(0, len(up) - 60),
                "down_n": len(g.down.get(nid, ())), "manual_ok": g.nodes[nid]["kind"] in ("device", "node", "container", "connection", "hub", "hub_device")}

    def candidates(self) -> list:
        """What a manual entry may point at: devices, infrastructure outside the inventory, containers, connections and the internet
        (a service that only works with the internet: the emergency scenario "Internet down" names it, S13)."""
        g = self.graph()
        out = [self.view(g, n) for n, i in g.nodes.items() if i["kind"] in ("device", "node", "container", "connection", "hub", "internet")
               and not i.get("hidden")]
        out.sort(key=lambda r: (KIND_RANK.get(r["kind"], 99), (r["title"] or r.get("ip") or r["id"]).casefold()))
        return out[:2000]

    def set_dep(self, ident: str, target: str, kind, note: str = "") -> dict:
        g = self.graph()
        nid = self.resolve(g, ident)
        tid = self.resolve(g, target)
        if nid is None or tid is None:
            raise ValueError("fail.err.unknown")
        if nid == tid:
            raise ValueError("fail.err.self")
        if g.nodes[nid]["kind"] not in ("device", "node", "container", "connection", "hub", "hub_device"):
            raise ValueError("fail.err.kind")
        set_manual(self.core.store, nid, tid, kind or None, note)
        self.invalidate()
        return {"ok": True, "node": nid, "target": tid, "k": kind or None}

    # --------------------------------------------------------------- the board: which tile needs which (hover)
    def board(self, tiles: set) -> dict:
        """Manual dependencies between tiles of one board (the drawn traces already show the rest): [[needs, needed, cert]]."""
        g = self.graph()
        out = []
        for a, b, why, cert, _v in g.edges:
            if not why.startswith("manual_"):
                continue
            ta = [x for x in self.view(g, a)["tiles"] if x in tiles][:1]
            tb = [x for x in self.view(g, b)["tiles"] if x in tiles][:1]
            if ta and tb and ta[0] != tb[0]:
                out.append([ta[0], tb[0], cert])
        return {"extra": out[:500]}
