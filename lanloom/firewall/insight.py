"""Firewall insight (read only): the rules of a read zone firewall in plain words, "May A reach B?", checks that come
with a suggestion, and the hit counters over time ("blocked today", hits of the last hour).

Everything here works on the neutral model v2 (``policies``, ``zones``, ``networks``, ``devices``) and never writes.
Texts are returned as ``{"key", "vars"}`` pairs (a var may itself be such a pair); the web client translates them.
"""
import ipaddress
import threading
import time

ANY = "*"
VERDICTS = ("allow", "block", "reject")
MAX_ITEMS_SHOWN = 3
IDS_LONG = 30                       # entries in the suppression list of the intrusion detection that make it "too long"
HIT_RING_S = 26 * 3600
HIT_STEP_S = 300                    # at most one snapshot of the counters every 5 minutes
HIT_STORE_KEY = "fw_hits:"
HIT_MAX_KEYS = 8                    # sources kept


def msg(key: str, **vars) -> dict:
    return {"key": key, "vars": vars}


# ---------------------------------------------------------------------------- the rules in plain words
def _zone_name(state: dict, zid) -> str:
    z = next((z for z in state.get("zones") or [] if z.get("id") == zid), None)
    return (z or {}).get("name") or "?"


def _net_name(state: dict, nid) -> str:
    n = next((n for n in state.get("networks") or [] if n.get("id") == nid), None)
    return (n or {}).get("name") or "?"


def _items_text(items) -> str:
    items = [str(i) for i in items or []]
    shown = ", ".join(items[:MAX_ITEMS_SHOWN])
    return shown + (" +%d" % (len(items) - MAX_ITEMS_SHOWN) if len(items) > MAX_ITEMS_SHOWN else "")


def side_match(side: dict):
    """The narrowing of a rule side: its ``match``, or the one network of a rule a family can change (``{"zone", "net"}``,
    the shape of a plan step) as a network match."""
    side = side or {}
    if side.get("match"):
        return side["match"]
    if side.get("net"):
        return {"kind": "network", "items": [side["net"]]}
    return None


def side_text(state: dict, side: dict) -> dict:
    """One side of a rule ("who" or "to what") as a message."""
    side = side or {}
    zid = side.get("zone")
    zone = msg("fw.plain.any_zone") if zid == ANY else msg("fw.plain.zone", zone=_zone_name(state, zid))
    m = side_match(side)
    if m:
        kind, items = m.get("kind"), m.get("items") or []
        if kind == "network":
            what = msg("fw.plain.net", names=_items_text(_net_name(state, i) for i in items))
        elif kind in ("ip", "domain"):
            what = msg("fw.plain.addr", items=_items_text(items))
        elif kind == "region":
            what = msg("fw.plain.region", items=_items_text(items))
        elif kind == "client":
            what = msg("fw.plain.clients", n=m.get("count") or len(items) or 0)
        elif kind == "app":
            what = msg("fw.plain.apps", n=m.get("count") or len(items) or 0)
        elif kind == "vpn":
            what = msg("fw.plain.vpn")
        else:
            what = msg("fw.plain.other_match")
        if m.get("opposite"):
            what = msg("fw.plain.not", what=what)
        zone = msg("fw.plain.in_zone", what=what, zone=_zone_name(state, zid)) if zid not in (None, ANY) else what
    if side.get("ports"):
        zone = msg("fw.plain.from_ports", what=zone, ports=str(side["ports"]))
    return zone


def service_text(pol: dict) -> dict:
    proto = str(pol.get("proto") or "any").lower()
    ports = str(pol.get("ports") or "")
    label = proto.upper().replace("_", "/") if proto != "any" else ""
    if not label and not ports:
        return msg("fw.plain.any_service")
    if ports:
        neg = ports.startswith("!")
        ports = ports.lstrip("!")
        if not label:
            return msg("fw.plain.ports_not" if neg else "fw.plain.ports", ports=ports)
        return msg("fw.plain.service_not" if neg else "fw.plain.service", proto=label, ports=ports)
    return msg("fw.plain.proto", proto=label)


def plain(state: dict, pol: dict) -> dict:
    """The rule as one sentence plus notes: ``{"key", "vars", "notes": [message]}``."""
    action = pol.get("action") if pol.get("action") in VERDICTS else "other"
    out = msg("fw.plain." + action, src=side_text(state, pol.get("src")), dst=side_text(state, pol.get("dst")),
              service=service_text(pol))
    notes = []
    if action == "allow" and pol.get("return_traffic"):
        notes.append(msg("fw.plain.note.return"))
    if pol.get("states"):
        notes.append(msg("fw.plain.note.states", states=", ".join(str(s) for s in pol["states"])))
    if pol.get("scheduled"):
        notes.append(msg("fw.plain.note.scheduled"))
    if pol.get("enabled") is False:
        notes.append(msg("fw.plain.note.off"))
    if pol.get("builtin"):
        notes.append(msg("fw.plain.note.builtin"))
    out["notes"] = notes
    return out


# ---------------------------------------------------------------------------- "May A reach B?"
def _net_of(state: dict, ip: str):
    best = None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for n in state.get("networks") or []:
        try:
            net = ipaddress.ip_network(n.get("cidr") or "", strict=False)
        except ValueError:
            continue
        if addr in net and (best is None or net.prefixlen > best[1].prefixlen):
            best = (n, net)
    return best[0] if best else None


def internet_zone(state: dict):
    """The zone that stands for "the internet": the zone flagged external, else the zone of a wan network."""
    for z in state.get("zones") or []:
        if z.get("system") == "external":
            return z["id"]
    for n in state.get("networks") or []:
        if n.get("purpose") == "wan" or n.get("internet") is True and n.get("wan"):
            return n.get("zone")
    return None


def resolve(state: dict, spec) -> dict:
    """What a question names: ``internet``, a network id, a zone id (``zone:<id>``), a device id or an address.
    Returns {"label", "zone", "network", "ip", "device"} or {"error": "unknown"}."""
    spec = str(spec or "").strip()
    if not spec:
        return {"error": "unknown"}
    if spec == "internet":
        z = internet_zone(state)
        return {"label": msg("fw.ask.internet"), "zone": z, "network": None, "ip": None, "device": None, "internet": True} if z else {"error": "no_internet_zone"}
    if spec.startswith("zone:"):
        z = next((z for z in state.get("zones") or [] if z.get("id") == spec[5:]), None)
        return {"label": z["name"], "zone": z["id"], "network": None, "ip": None, "device": None} if z else {"error": "unknown"}
    n = next((n for n in state.get("networks") or [] if n.get("id") == spec), None)
    if n:
        return {"label": n.get("name") or "?", "zone": n.get("zone"), "network": n["id"], "ip": None, "device": None}
    d = next((d for d in state.get("devices") or [] if d.get("id") == spec), None)
    if d is None:
        try:
            ip = str(ipaddress.ip_address(spec))
        except ValueError:
            return {"error": "unknown"}
        d = next((d for d in state.get("devices") or [] if d.get("ip") == ip), None)
        net = _net_of(state, ip)
        if d is None:
            if net is None:                      # an address outside every network: the internet side
                z = internet_zone(state)
                return {"label": ip, "zone": z, "network": None, "ip": ip, "device": None, "internet": True} if z else {"error": "unknown"}
            return {"label": ip, "zone": net.get("zone"), "network": net["id"], "ip": ip, "device": None}
    net = next((n for n in state.get("networks") or [] if n.get("id") == d.get("network")), None)
    return {"label": d.get("name") or d.get("ip") or "?", "zone": (net or {}).get("zone"), "network": d.get("network"), "ip": d.get("ip"),
            "device": d["id"]}


def _ip_in(ip: str, items) -> object:
    """True/False when every item is an address, a range or a network; None when one is not understood."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    unknown = False
    for it in items or []:
        it = str(it).strip()
        try:
            if "-" in it:
                a, b = (ipaddress.ip_address(x.strip()) for x in it.split("-", 1))
                if a <= addr <= b:
                    return True
            elif "/" in it:
                if addr in ipaddress.ip_network(it, strict=False):
                    return True
            elif ipaddress.ip_address(it) == addr:
                return True
        except ValueError:
            unknown = True
    return None if unknown else False


def _port_in(spec: str, port: int):
    """True/False for a port list like ``80,443,8000-8100``; None when it names something else (a group)."""
    hit = False
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                hit = hit or int(a) <= port <= int(b)
            else:
                hit = hit or int(part) == port
        except ValueError:
            return None
    return hit


def _side_matches(side: dict, who: dict):
    """True, False or None (cannot tell) for one rule side against what a question names."""
    zid = side.get("zone")
    if zid not in (ANY, who.get("zone")):
        return False
    m = side_match(side)
    if not m:
        return True
    kind, items = m.get("kind"), m.get("items") or []
    if kind == "network":
        if not who.get("network"):
            res = None if who.get("zone") else False
        else:
            res = who["network"] in items
    elif kind == "ip":
        res = _ip_in(who["ip"], items) if who.get("ip") else None
    else:
        res = None
    if res is None:
        return None
    return (not res) if m.get("opposite") else res


def _service_matches(pol: dict, proto, port):
    p = str(pol.get("proto") or "any").lower()
    ports = str(pol.get("ports") or "")
    if p != "any" and proto:
        if proto not in p.split("_") and p != proto:
            return False
    elif p != "any" and not proto:
        return None
    if not ports:
        return True
    if ports == "list" or not port:
        return None
    neg = ports.startswith("!")
    res = _port_in(ports.lstrip("!"), int(port))
    if res is None:
        return None
    return (not res) if neg else res


def _pol_verdict(pol: dict):
    return pol.get("action") if pol.get("action") in VERDICTS else "block"


def ask(state: dict, src, dst, proto=None, port=None) -> dict:
    """"May ``src`` reach ``dst``?" for a new connection, from the rules in order (first definite match wins).

    ``src`` / ``dst``: see ``resolve``. ``proto`` ``tcp``/``udp``/None, ``port`` an int or None. Rules that cannot be
    judged (a match on clients, apps, regions, a schedule, a port group) make the answer uncertain and are listed
    in ``maybe``. Nothing is sent anywhere."""
    a, b = resolve(state, src), resolve(state, dst)
    for who in (a, b):
        if who.get("error"):
            return {"error": who["error"]}
    proto = str(proto).lower() if proto else None
    if proto not in (None, "tcp", "udp"):
        proto = None
    try:
        port = int(port) if port not in (None, "") else None
    except (TypeError, ValueError):
        port = None
    if port is not None and not 0 < port < 65536:
        port = None
    out = {"src": {k: v for k, v in a.items() if k != "error"}, "dst": {k: v for k, v in b.items() if k != "error"},
           "proto": proto, "port": port, "maybe": [], "certain": True}
    if a.get("zone") == b.get("zone") and a.get("zone") and not a.get("internet") and not b.get("internet"):
        same_net = a.get("network") and a.get("network") == b.get("network")
        if same_net:
            out.update(verdict="allow", reason=msg("fw.ask.same_network"), rule=None, default=True,
                       verdict_label=msg("fw.ask.v.allow"))
            return out
    pols = sorted((p for p in state.get("policies") or [] if p.get("enabled", True)),
                  key=lambda p: (p.get("order", 0), str(p.get("id"))))
    for p in pols:
        ms = [_side_matches(p.get("src") or {}, a), _side_matches(p.get("dst") or {}, b), _service_matches(p, proto, port)]
        if False in ms:
            continue
        if p.get("states") and "new" not in [str(s).lower() for s in p["states"]]:
            continue                                   # about return traffic or invalid packets, not a new connection
        uncertain = None in ms or bool(p.get("scheduled"))
        if uncertain:
            out["maybe"].append({"id": p.get("id"), "name": p.get("name"), "verdict": _pol_verdict(p), "plain": plain(state, p),
                                 "why": msg("fw.ask.maybe_schedule" if p.get("scheduled") and None not in ms else "fw.ask.maybe_match")})
            continue
        out.update(verdict=_pol_verdict(p), rule={"id": p.get("id"), "name": p.get("name"), "builtin": bool(p.get("builtin")),
                                                  "plain": plain(state, p)},
                   reason=msg("fw.ask.by_rule", rule=p.get("name") or "?"), default=False)
        break
    else:
        pair = [a.get("zone"), b.get("zone")]
        if a.get("zone") and a.get("zone") == b.get("zone") and not a.get("internet") and not b.get("internet"):
            # within one zone the controllers let traffic through unless a rule says otherwise (the board's diagonal)
            out.update(verdict="allow", rule=None, default=True, reason=msg("fw.ask.same_zone"))
        else:
            ok = pair in (state.get("allow") or [])
            out.update(verdict="allow" if ok else "block", rule=None, default=True, reason=msg("fw.ask.by_default"))
    if out["maybe"]:                                   # every rule listed here comes before the deciding one
        out["certain"] = False
        if any(m["verdict"] != out["verdict"] for m in out["maybe"]):
            out["verdict"] = "depends"
    out["verdict_label"] = msg("fw.ask.v." + out["verdict"])
    return out


# ---------------------------------------------------------------------------- checks with a suggestion
SUGGEST = {"admin_on_wan": "fwr.suggest.admin_on_wan", "any_any": "fwr.suggest.any_any", "wan_inbound_open": "fwr.suggest.wan_inbound_open",
           "rules_disabled": "fwr.suggest.rules_disabled", "ids_suppression_long": "fwr.suggest.ids_suppression_long",
           "shadowed": "fwr.suggest.shadowed", "guest_reaches_inside": "fwr.suggest.guest_reaches_inside"}


def _whole(p: dict) -> bool:
    return (not side_match(p.get("src")) and not side_match(p.get("dst")) and not p.get("ports")
            and str(p.get("proto") or "any").lower() == "any" and not p.get("states") and not p.get("scheduled")
            and not (p.get("src") or {}).get("ports") and not (p.get("dst") or {}).get("ports"))


def _covers(a: dict, b: dict) -> bool:
    """True when whole-pair rule ``a`` decides everything rule ``b`` could decide."""
    if not _whole(a):
        return False
    for side in ("src", "dst"):
        za, zb = (a.get(side) or {}).get("zone"), (b.get(side) or {}).get("zone")
        if za != ANY and za != zb:
            return False
    return True


def findings(state: dict) -> list:
    """Checks of the read rules: the adapter's own findings plus those only the rules can tell, each with a
    ``suggestion`` message key. Nothing is changed."""
    out = [dict(f) for f in state.get("findings") or [] if isinstance(f, dict)]
    pols = sorted((p for p in state.get("policies") or []), key=lambda p: (p.get("order", 0), str(p.get("id"))))
    on = [p for p in pols if p.get("enabled", True)]
    seen = set()
    for i, b in enumerate(on):
        if b.get("builtin") or b.get("id") in seen:
            continue
        a = next((a for a in on[:i] if _covers(a, b)), None)
        if a is not None:
            seen.add(b["id"])
            out.append({"id": "x_shadow_" + str(b["id"]), "kind": "shadowed", "level": "warn", "subject": b["id"],
                        "params": {"rule": b.get("name") or "?", "by": a.get("name") or "?",
                                   "same": _pol_verdict(a) == _pol_verdict(b)}})
    untrusted = {z["id"] for z in state.get("zones") or [] if z.get("system") == "hotspot" or z.get("kind") == "guest"}
    for n in state.get("networks") or []:
        if n.get("purpose") == "guest" and n.get("zone"):
            untrusted.add(n["zone"])
    inside = {z["id"] for z in state.get("zones") or [] if z.get("system") == "internal" or z.get("kind") == "internal"}
    for p in on:
        s, d = p.get("src") or {}, p.get("dst") or {}
        if p.get("action") == "allow" and s.get("zone") in untrusted and d.get("zone") in inside and not side_match(s) and not side_match(d) \
                and not p.get("ports") and str(p.get("proto") or "any").lower() == "any" and not p.get("builtin"):
            out.append({"id": "x_guest_" + str(p["id"]), "kind": "guest_reaches_inside", "level": "alert", "subject": p["id"],
                        "params": {"rule": p.get("name") or "?", "from": _zone_name(state, s["zone"]), "to": _zone_name(state, d["zone"])}})
    ids = state.get("ids") or {}
    n_sup = ids.get("suppressions")
    if isinstance(n_sup, int) and not isinstance(n_sup, bool) and n_sup >= IDS_LONG:
        out.append({"id": "x_ids_long", "kind": "ids_suppression_long", "level": "warn", "subject": None, "params": {"n": n_sup, "limit": IDS_LONG}})
    for f in out:
        f["suggestion"] = SUGGEST.get(f.get("kind"))
    order = {"alert": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: (order.get(f.get("level"), 3), str(f.get("kind"))))
    return out


# ---------------------------------------------------------------------------- the board (zones as pads, hits as traffic)
def pair_key(a, b) -> str:
    return "%s>%s" % (a, b)


def board(state: dict, hour: dict = None, blocked_hour: dict = None) -> dict:
    """Data of the firewall board: one pad per zone, one line per zone pair that may talk or that has rules of its own,
    with the number of hits of the last hour (``hour``: {policy id: hits}). Built-in default rules do not count as
    "own"."""
    zones = [{"id": z["id"], "name": z.get("name") or "?", "internet": bool(z.get("internet")), "system": z.get("system"),
              "networks": [n.get("name") or "?" for n in state.get("networks") or [] if n.get("zone") == z["id"]][:4]}
             for z in state.get("zones") or []]
    allow = {pair_key(a, b) for a, b in state.get("allow") or []}
    own, hits, blocks = {}, {}, {}
    for p in state.get("policies") or []:
        s, d = (p.get("src") or {}).get("zone"), (p.get("dst") or {}).get("zone")
        k = pair_key(s, d)
        if not p.get("builtin"):
            own[k] = own.get(k, 0) + 1
        n = (hour or {}).get(p.get("id"))
        if n:
            if p.get("action") == "allow":
                hits[k] = hits.get(k, 0) + n
            else:
                blocks[k] = blocks.get(k, 0) + n
    pairs = []
    for a in zones:
        for b in zones:
            if a["id"] == b["id"]:
                continue
            k = pair_key(a["id"], b["id"])
            if k in allow or own.get(k) or hits.get(k) or blocks.get(k):
                pairs.append({"from": a["id"], "to": b["id"], "allow": k in allow, "own": own.get(k, 0), "hits": hits.get(k, 0),
                              "blocked": blocks.get(k, 0)})
    return {"zones": zones, "pairs": pairs}


# ---------------------------------------------------------------------------- hit counters over time
def _local_day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


class HitLog:
    """Keeps snapshots of the policy hit counters per source (every 5 minutes, 26 hours) and the first snapshot of the
    local day, and answers "hits in the last hour" and "blocked today". A counter that went down (the gateway
    restarted) counts from zero again. Kept in the store, so a restart of Lanloom keeps the day."""

    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._mem = {}

    def _load(self, key: str) -> dict:
        if key not in self._mem:
            raw = self.store.get_setting(HIT_STORE_KEY + key, None)
            self._mem[key] = raw if isinstance(raw, dict) else {"day": None, "base": {}, "base_t": 0, "ring": [], "saved": 0}
        return self._mem[key]

    def feed(self, key: str, policies, now: float = None) -> None:
        now = time.time() if now is None else now
        snap = {str(p["id"]): int(p["hits"]) for p in policies or [] if isinstance(p.get("hits"), int) and not isinstance(p.get("hits"), bool)}
        if not snap:
            return
        with self._lock:
            d = self._load(key)
            if d["ring"] and now - d["ring"][-1][0] < HIT_STEP_S and d["day"] == _local_day(now):
                return
            if d["day"] != _local_day(now):
                d["day"], d["base"], d["base_t"] = _local_day(now), snap, now
            d["ring"].append([now, snap])
            d["ring"] = [r for r in d["ring"] if now - r[0] <= HIT_RING_S][-400:]
            if now - d["saved"] >= HIT_STEP_S:
                d["saved"] = now
                try:
                    self.store.set_setting(HIT_STORE_KEY + key, d)
                except Exception:
                    pass
            if len(self._mem) > HIT_MAX_KEYS:
                self._mem.pop(next(iter(self._mem)), None)

    @staticmethod
    def _delta(now_snap: dict, then: dict) -> dict:
        out = {}
        for pid, n in now_snap.items():
            before = then.get(pid)
            out[pid] = n if before is None or n < before else n - before
        return out

    def summary(self, key: str, policies, now: float = None) -> dict:
        """{"day": {pid: n}, "hour": {pid: n}, "day_since": t, "hour_ok": bool, "blocked_today": n, "top": [...]}"""
        now = time.time() if now is None else now
        with self._lock:
            d = self._load(key)
            cur = {str(p["id"]): int(p["hits"]) for p in policies or [] if isinstance(p.get("hits"), int) and not isinstance(p.get("hits"), bool)}
            if not cur or d["day"] != _local_day(now):
                return {"day": {}, "hour": {}, "day_since": None, "hour_ok": False, "blocked_today": None, "top": []}
            day = self._delta(cur, d["base"])
            ref = None
            for t, snap in d["ring"]:
                if now - t <= 3600 + HIT_STEP_S and (ref is None or abs(now - 3600 - t) < abs(now - 3600 - ref[0])):
                    ref = (t, snap)
            hour_ok = ref is not None and now - ref[0] >= 1800
            hour = self._delta(cur, ref[1]) if hour_ok else {}
            base_t = d["base_t"]
        names = {str(p["id"]): p for p in policies or []}
        blocked = {pid: n for pid, n in day.items() if n and (names.get(pid) or {}).get("action") in ("block", "reject")}
        top = sorted(blocked.items(), key=lambda kv: -kv[1])[:5]
        return {"day": day, "hour": hour, "day_since": base_t, "hour_ok": hour_ok, "blocked_today": sum(blocked.values()),
                "top": [{"id": pid, "name": names[pid].get("name") or "?", "n": n} for pid, n in top]}
