"""Building blocks of the nightly report: every section is a contribution that registers itself.

A contribution has an id, a sort order, a title key and two functions: ``available(ctx)`` (is the source there at
all? if not, the section is left out) and ``build(ctx)`` (the section). Later features add their own contribution
with ``register(Contribution(...))``; nothing else needs to change.

A section is ``{"id", "title", "level", "summary": {"key", "vars"}, "items": [item, ...], "more": n}`` and an item is
``{"id", "key", "vars", "level": "ok"|"info"|"warn"|"danger", "point": bool}``. ``point`` items are the things that
need a look; they count in the headline and can be ticked off. Texts are i18n keys; variables ending in ``_at`` are
timestamps (shown in the report's time zone), ``_dur`` durations in seconds, ``_bytes`` byte counts, ``_cc`` country codes
(comma-separated), ``_key`` i18n keys; the placeholder in the text is the name without the suffix (``from_at`` -> ``{from}``).
Item ids are stable (for the tick marks) and never shown.
"""
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from typing import Callable, Optional

from .journal import INFRA_KINDS, intervals

MAX_ITEMS = 12           # per section; the rest is counted as "and N more"
SHORT_BLIP_S = 60        # an uplink flap shorter than this is still listed, but not as a point
CERT_WARN_DAYS = 30
CERT_POINT_DAYS = 14
DISK_LOW_SHARE = 0.10
DISK_LOW_BYTES = 1_000_000_000
STALE_SOURCE_S = 1800
CFG_LISTS = ("policies", "networks", "ssids", "port_forwards", "zones")


@dataclass
class Contribution:
    id: str
    order: int
    title: str
    available: Callable
    build: Callable


REGISTRY: list = []


def register(contribution: Contribution) -> Contribution:
    REGISTRY[:] = [c for c in REGISTRY if c.id != contribution.id] + [contribution]
    REGISTRY.sort(key=lambda c: c.order)
    return contribution


@dataclass
class Ctx:
    """Everything a contribution may read. ``core`` is the application core; the rest is prepared by the builder."""
    core: object
    journal: object
    since: float
    until: float
    prev: Optional[dict]                 # the previous saved report (or None)
    certs: list = field(default_factory=list)      # [{"id", "name", "days"|None, "key"?}] checked by the builder
    update: dict = field(default_factory=dict)     # update state (current, latest, available)
    data_dir: object = None
    snapshot: dict = field(default_factory=dict)   # controller configuration seen now (filled by the controller section)
    families: dict = field(default_factory=dict)   # family states seen now (filled by the family sections)
    keep: dict = field(default_factory=dict)       # small state a section compares with next night ({section id: ...})

    def events(self, *kinds) -> list:
        return self.journal.between(self.since, self.until, kinds or None)


def item(iid: str, key: str, level: str = "info", point: bool = False, **vars_) -> dict:
    return {"id": iid, "key": key, "vars": vars_, "level": level, "point": bool(point)}


def section(cid: str, title: str, summary_key: str, items: list, parts: Optional[list] = None, name: str = "", **vars_) -> dict:
    order = {"danger": 0, "warn": 1, "info": 2, "ok": 3}
    items = sorted(items, key=lambda i: (0 if i["point"] else 1, order.get(i["level"], 2)))
    level = "ok"
    for i in items:
        if i["point"] and order[i["level"]] < order[level]:
            level = i["level"]
    # points beyond the visible list still count and stay tickable; only plain notes are folded into "and N more"
    shown = items[:MAX_ITEMS] + [i for i in items[MAX_ITEMS:] if i["point"]]
    summary = {"key": summary_key, "vars": vars_}
    if parts:
        summary["parts"] = parts            # shown after the summary, joined with " · "
    out = {"id": cid, "title": title, "level": level, "summary": summary, "items": shown, "more": len(items) - len(shown)}
    if name:
        out["name"] = name                  # the connection a section belongs to, shown next to the title
    return out


def _short(text, n=60) -> str:
    text = str(text or "").strip()
    return text if len(text) <= n else text[:n - 1] + "…"


# ---------------------------------------------------------------------- internet
def _internet_available(ctx: Ctx) -> bool:
    core = ctx.core
    if ctx.journal.any(("wan_down", "wan_up", "wan_switch")):
        return True
    return any(ev.get("host") == core.gateway_ip for ev in core.snmp.values()) if core.gateway_ip else False


def _internet(ctx: Ctx) -> dict:
    evs = ctx.events("wan_down", "wan_up", "wan_switch")
    first = ctx.journal.last_before(ctx.since, ("wan_down", "wan_up"))
    down_at_start = bool(first and first["kind"] == "wan_down")
    items, total = [], 0.0
    for start, end, open_ in intervals(evs, "wan_down", "wan_up", ctx.since, ctx.until, down_at_start):
        dur = max(0.0, end - start)
        total += dur
        if open_:
            items.append(item("wan:%d" % start, "report.internet.down_now", "danger", True, from_at=start, dur_dur=dur))
        else:
            big = dur >= SHORT_BLIP_S
            items.append(item("wan:%d" % start, "report.internet.outage", "warn" if big else "info", big,
                              from_at=start, to_at=end, dur_dur=dur))
    for ev in evs:
        if ev["kind"] == "wan_switch":
            d = ev["data"]
            items.append(item("wansw:%d" % ev["t"], "report.internet.switch", "info", False, at_at=ev["t"],
                              to=_short(d.get("to")), was=_short(d.get("from"))))
    outages = sum(1 for i in items if i["key"] != "report.internet.switch")
    if not outages:
        return section("internet", "report.sec.internet", "report.internet.none", items)
    return section("internet", "report.sec.internet", "report.internet.summary", items, n=outages, dur_dur=total)


# ---------------------------------------------------------------------- devices
NEW_BY_ORIGIN = {"random": "report.devices.new_random", "unregistered": "report.devices.new_unregistered"}

def _devices_available(ctx: Ctx) -> bool:
    return ctx.core.store.one("SELECT 1 AS x FROM devices LIMIT 1") is not None


def _devices(ctx: Ctx) -> dict:
    evs = ctx.events("dev_new", "dev_offline", "dev_back", "dev_gone")
    items, new, gone, flaps = [], 0, 0, {}
    current = {d["id"]: d for d in ctx.core.store.devices()}
    for ev in evs:
        d = ev["data"]
        did, title = d.get("id") or "", _short(d.get("title"))
        if did in current:                  # a rename since then shows the new name
            cur = current[did]
            title = _short(cur.get("user_title") or cur["title"] or title)
        if ev["kind"] == "dev_new":
            new += 1
            origin = d.get("origin") or ""
            if d.get("vendor"):
                items.append(item("new:%s" % did, "report.devices.new_vendor", "warn", True, name=title, ip=d.get("ip") or "",
                                  vendor=_short(d.get("vendor")), at_at=ev["t"]))
            elif origin in NEW_BY_ORIGIN:
                items.append(item("new:%s" % did, NEW_BY_ORIGIN[origin], "warn", True, name=title, ip=d.get("ip") or "",
                                  at_at=ev["t"]))
            else:
                items.append(item("new:%s" % did, "report.devices.new", "warn", True, name=title, ip=d.get("ip") or "", at_at=ev["t"]))
        elif ev["kind"] == "dev_gone":
            gone += 1
            items.append(item("gone:%s:%d" % (did, d.get("since") or ev["t"]), "report.devices.gone", "warn", True,
                              name=title, since_at=d.get("since") or ev["t"]))
        else:
            f = flaps.setdefault(did, {"title": title, "kind": d.get("kind") or "", "off": [], "back": []})
            f["off" if ev["kind"] == "dev_offline" else "back"].append(ev["t"])
    still = 0
    for did, f in flaps.items():
        infra = f["kind"] in INFRA_KINDS
        cur = current.get(did)
        now_down = bool(cur) and ctx.core.device_status(cur) == "down" and f["off"] and (not f["back"] or f["back"][-1] < f["off"][-1])
        if now_down:
            still += 1
            items.append(item("off:%s:%d" % (did, f["off"][-1]), "report.devices.still_offline", "warn" if infra else "info", infra,
                              name=f["title"], at_at=f["off"][-1]))
        elif len(f["off"]) == 1 and f["back"]:
            items.append(item("flap:%s" % did, "report.devices.offline_back", "info", False, name=f["title"],
                              from_at=f["off"][0], to_at=f["back"][-1]))
        elif f["off"]:
            items.append(item("flap:%s" % did, "report.devices.offline_times", "info", False, name=f["title"], n=len(f["off"]),
                              from_at=f["off"][0], to_at=(f["back"] or f["off"])[-1]))
        else:
            items.append(item("flap:%s" % did, "report.devices.back", "info", False, name=f["title"], at_at=f["back"][-1]))
    total = len([d for d in current.values() if not d["hidden"]])
    if not items:
        return section("devices", "report.sec.devices", "report.devices.calm", items, n=total)
    parts = [{"key": "report.devices.part." + k, "vars": {"n": v}} for k, v in (("new", new), ("gone", gone),
                                                                              ("still", still), ("flaps", len(flaps) - still)) if v]
    return section("devices", "report.sec.devices", "report.devices.summary", items, parts=parts, n=total)


# ---------------------------------------------------------------------- connections (availability of the integrations)
def _integrations(ctx: Ctx) -> list:
    return [i for i in ctx.core.integrations() if i.get("enabled")]


def _src_periods(ctx: Ctx, key: str) -> list:
    evs = [e for e in ctx.events("src_fail", "src_ok") if e["data"].get("id") == key]
    first = ctx.journal.last_before(ctx.since, ("src_fail", "src_ok"), lambda d: d.get("id") == key)
    return intervals(evs, "src_fail", "src_ok", ctx.since, ctx.until, bool(first and first["kind"] == "src_fail")), evs


def _connections(ctx: Ctx) -> dict:
    items, span = [], max(1.0, ctx.until - ctx.since)
    worst = 1.0
    for integ in _integrations(ctx):
        key = "int:%d" % integ["id"]
        periods, evs = _src_periods(ctx, key)
        down = sum(e - s for s, e, _ in periods)
        share = max(0.0, 1.0 - down / span)
        worst = min(worst, share)
        if not periods:
            continue
        open_ = periods[-1][2]
        if open_ and periods[-1][1] - periods[-1][0] >= STALE_SOURCE_S:
            items.append(item("src:%s:%d" % (key, periods[-1][0]), "report.conn.down_now", "warn", True,
                              name=_short(integ["name"]), from_at=periods[-1][0]))
        else:
            items.append(item("src:%s" % key, "report.conn.interrupted", "info", False, name=_short(integ["name"]),
                              n=len(periods), dur_dur=down, pct=_pct(share)))
    if not items:
        return section("connections", "report.sec.connections", "report.conn.calm", items, n=len(_integrations(ctx)))
    return section("connections", "report.sec.connections", "report.conn.summary", items, n=len(items), pct=_pct(worst))


def _pct(share: float):
    """Percent as a number (the reader writes it in its own language): one decimal just below 100, otherwise whole."""
    v = share * 100
    return round(v, 1) if 99 < v < 100 else int(round(v))


# ---------------------------------------------------------------------- integrations refusing the sign-in
LOGIN_REASONS = ("auth_failed", "forbidden")


def _refused(reason: str) -> bool:
    return any(str(reason or "").endswith("." + r) for r in LOGIN_REASONS)


def _logins(ctx: Ctx) -> dict:
    items = []
    for integ in _integrations(ctx):
        key = "int:%d" % integ["id"]
        st = ctx.core.source_status.get(key) or {}
        seen = [e for e in ctx.events("src_fail") if e["data"].get("id") == key and _refused(e["data"].get("key"))]
        if not st.get("ok", True) and _refused(st.get("key")):
            items.append(item("login:%s:%s" % (key, st.get("key")), "report.login.refused", "danger", True,
                              name=_short(integ["name"]), reason_key=st.get("key")))
        elif seen:
            items.append(item("login:%s:%d" % (key, seen[0]["t"]), "report.login.refused_once", "info", False,
                              name=_short(integ["name"]), at_at=seen[0]["t"]))
    refusing = len([i for i in items if i["point"]])
    key = "report.login.summary" if refusing else ("report.login.recovered" if items else "report.login.calm")
    return section("logins", "report.sec.logins", key, items, n=refusing)


# ---------------------------------------------------------------------- data paths (S14)
def _paths_available(ctx: Ctx) -> bool:
    return ctx.core.store.one("SELECT 1 AS x FROM paths_hour LIMIT 1") is not None


def _paths(ctx: Ctx) -> dict:
    s = ctx.core.paths_summary("24h")
    titles = {d["id"]: (d.get("title") or "") for d in s.get("devices") or []}
    items = []
    for u in s.get("unusual") or []:
        title = _short(titles.get(u.get("device")) or "")
        name = {"name": title} if title else {"name_key": "report.this_host"}
        if u["kind"] == "new_country":
            items.append(item("path:new:%s:%s" % (u["device"], u["cc"]), "report.paths.new_country", "warn", True,
                              place_cc=u["cc"], amount_bytes=u["bytes"], **name))
        elif u["kind"] == "heavy":
            items.append(item("path:heavy:%s" % u["device"], "report.paths.heavy", "warn", True,
                              amount_bytes=u["bytes"], typical_bytes=u.get("typical") or 0, **name))
        elif u["kind"] == "offline_net":
            items.append(item("path:offnet:%s" % u["device"], "report.paths.offline_net", "danger", True,
                              net=u.get("network") or "", **name))
    city = ctx.core.geo.city                                    # cities and operators (an option, off by default)
    if city.enabled and not city.demo:
        if city.state["error"] and city.file is None:
            items.append(item("path:detail:error", "report.paths.detail_error", "warn", True))
        elif city.state["error"] and city.file is not None:
            items.append(item("path:detail:stale", "report.paths.detail_stale", "info", False, month=city.file.month))
        elif city.state["updated"] and ctx.until - city.state["updated"] < 86400 and city.file is not None:
            items.append(item("path:detail:updated", "report.paths.detail_updated", "info", False, month=city.file.month))
    rw = getattr(ctx.core, "routes", None)                     # network routes (an option, off by default): calm notes only
    if rw is not None and (rw.settings()["enabled"] or rw.demo):
        for r in rw.changes_between(ctx.since, ctx.until)[:5]:
            target = r.get("operator") or ("AS%d" % r["asn"] if r.get("asn") else "")
            items.append(item("path:route:%s" % r["key"], "report.paths.route_changed_anycast" if r.get("anycast") else "report.paths.route_changed",
                              "info", False, target=target or "-", place_cc=r.get("cc") or "", city=r.get("city") or ""))
        st = rw.state
        if rw.settings()["enabled"] and not rw.demo and rw.caps is not None and not any(rw.caps["methods"].values()):
            items.append(item("path:route:impossible", "report.paths.routes_impossible", "info", False))
        elif st.get("skipped") == "load" and st.get("last_run") is None:
            items.append(item("path:route:busy", "report.paths.routes_busy", "info", False))
    tot = s.get("totals") or {}
    share = tot.get("outside_eu_share")
    top = [c["cc"] for c in (s.get("countries") or [])[:3]]
    if not tot.get("attributable"):
        return section("paths", "report.sec.paths", "report.paths.empty", items)
    return section("paths", "report.sec.paths", "report.paths.summary", items, n=tot.get("countries") or 0,
                   amount_bytes=tot.get("attributable") or 0, pct=_pct(share) if share is not None else "–",
                   top_cc=",".join(top))


# ---------------------------------------------------------------------- controller configuration (U1)
def _controller_available(ctx: Ctx) -> bool:
    return bool(ctx.core.live_configs())


def _digest(obj) -> str:
    clean = {k: v for k, v in obj.items() if k not in ("hits", "last_hit", "status", "stats", "online", "cpu", "mem", "link", "speed")}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode()).hexdigest()[:16]


def controller_snapshot(core) -> dict:
    out = {}
    for cfg in core.live_configs():
        st = cfg.get("state") or {}
        snap = {lst: {str(o.get("id")): [_digest(o), _short(o.get("name") or "", 40)] for o in st.get(lst) or [] if isinstance(o, dict)}
                for lst in CFG_LISTS}
        snap["firmware"] = {str(e.get("id")): [e.get("firmware") or "", _short(e.get("name") or "", 40)]
                            for e in st.get("equipment") or [] if isinstance(e, dict)}
        out[cfg["key"]] = {"name": cfg.get("name") or "", "lists": snap}
    return out


def _controller(ctx: Ctx) -> dict:
    now = controller_snapshot(ctx.core)
    ctx.snapshot = now
    before = (ctx.prev or {}).get("controller_snapshot") or {}
    items, first = [], not before
    for key, cur in now.items():
        old = (before.get(key) or {}).get("lists")
        if old is None:
            continue
        for lst in CFG_LISTS:
            a, b = old.get(lst) or {}, cur["lists"].get(lst) or {}
            added = [b[i][1] for i in b if i not in a]
            removed = [a[i][1] for i in a if i not in b]
            changed = [b[i][1] for i in b if i in a and a[i][0] != b[i][0]]
            if added or removed or changed:
                names = ", ".join(_short(n, 30) for n in (added + changed + removed)[:3] if n) or "–"
                kinds = [k for k, v in (("added", added), ("changed", changed), ("removed", removed)) if v]
                tkey = "report.ctl." + (kinds[0] if len(kinds) == 1 else "mixed")
                items.append(item("cfg:%s:%s:%s" % (key, lst, _digest({"b": sorted(b), "c": [b[i][0] for i in sorted(b)]})),
                                  tkey, "warn", True, source=_short(cur["name"]), area_key="report.ctl.area." + lst,
                                  added=len(added), removed=len(removed), changed=len(changed), n=len(added + changed + removed),
                                  names=names))
        fw_old = old.get("firmware") or {}
        for eid, (fw, name) in (cur["lists"].get("firmware") or {}).items():
            was = (fw_old.get(eid) or [None])[0]
            if was and fw and was != fw:
                items.append(item("fw:%s:%s:%s" % (key, eid, fw), "report.ctl.firmware", "info", False, name=name or "–",
                                  version=_short(fw, 24), was=_short(was, 24)))
    if first:
        return section("controller", "report.sec.controller", "report.ctl.baseline", items, n=len(now))
    return section("controller", "report.sec.controller", "report.ctl.calm" if not items else "report.ctl.summary", items,
                   n=len([i for i in items if i["point"]]))


# ---------------------------------------------------------------------- sections of device families (integrations.night_sections)
FAMILY_STATE_DROP = ("devices", "ports", "policies", "guest_rules", "guest_nics", "raw", "dhcp_reservations")
FAMILY_LEVELS = {"alert": ("danger", True), "warn": ("warn", True), "info": ("info", False)}


def _family_fns() -> dict:
    from .. import integrations
    try:
        return dict(integrations.night_sections())
    except Exception:
        return {}


def family_states(core, until: float) -> dict:
    """What a family section needs from tonight's state next time (big lists left out), per connection."""
    fns = _family_fns()
    out = {}
    for cfg in core.live_configs():
        if cfg.get("family") in fns and isinstance(cfg.get("state"), dict):
            st = {k: v for k, v in cfg["state"].items() if k not in FAMILY_STATE_DROP}
            st["t"] = until
            out[cfg["key"]] = {"family": cfg["family"], "state": st}
    return out


def _families_available(ctx: Ctx) -> bool:
    fns = _family_fns()
    return any(cfg.get("family") in fns for cfg in ctx.core.live_configs())


def _families(ctx: Ctx) -> list:
    fns = _family_fns()
    before = (ctx.prev or {}).get("family_states") or {}
    ctx.families = family_states(ctx.core, ctx.until)
    out = []
    for cfg in ctx.core.live_configs():
        fn = fns.get(cfg.get("family"))
        if not fn:
            continue
        prev = (before.get(cfg["key"]) or {}).get("state")
        fam = fn(prev, cfg["state"], ctx.until, cfg.get("name"))
        if not fam:
            continue
        items = []
        for it in fam.get("items") or []:
            level, point = FAMILY_LEVELS.get(it.get("level"), ("info", False))
            params = {k: v for k, v in (it.get("params") or {}).items() if isinstance(v, (str, int, float))}
            iid = "fam:%s:%s:%s" % (cfg["key"], it.get("key"), hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:10])
            items.append(item(iid, it.get("key") or "", level, point, **params))
        params = {k: v for k, v in (fam.get("params") or {}).items() if isinstance(v, (str, int, float))}
        out.append(section("family:%s" % cfg["key"], fam.get("title") or "report.sec.controller", fam.get("summary") or "",
                           items, name=_short(cfg.get("name") or ""), **params))
    return out


# ---------------------------------------------------------------------- DNS filters (totals only, never single queries)
DNS_LEVELS = {"alert": ("danger", True), "warn": ("warn", True), "info": ("info", False)}


def _dns_filters(ctx: Ctx) -> list:
    from .. import dnsfilter
    try:
        return [v for v in (dnsfilter.filter_view(ctx.core, i["id"], ctx.until) for i in ctx.core.integrations()
                            if i["kind"] in ctx.core.dns_kinds()) if v]
    except Exception:
        return []


def _dns_available(ctx: Ctx) -> bool:
    return bool(_dns_filters(ctx))


def _dns(ctx: Ctx) -> dict:
    items, queries, blocked = [], 0, 0
    filters = _dns_filters(ctx)
    for v in filters:
        name = _short(v["name"])
        if v["health"] in ("down", "stale"):
            items.append(item("dns:%d:down" % v["integ"], "report.dns.down", "danger", True, name=name))
        tot = v.get("totals") or {}
        queries += tot.get("queries") or 0
        blocked += tot.get("blocked") or 0
        for f in v["findings"]:
            level, point = DNS_LEVELS.get(f["level"], ("info", False))
            params = {k: x for k, x in (f.get("params") or {}).items() if isinstance(x, (str, int, float))}
            items.append(item("dns:%d:%s" % (v["integ"], f["kind"]), f["report"], level, point, name=name, **params))
    points = len([i for i in items if i["point"]])
    return section("dns", "report.sec.dns", "report.dns.summary" if points else "report.dns.calm", items,
                   n=points, filters=len(filters), queries=queries, blocked=blocked, pct=_pct(blocked / queries) if queries else 0)


# ---------------------------------------------------------------------- certificates
def _certs_available(ctx: Ctx) -> bool:
    return bool(ctx.certs)


def _certs(ctx: Ctx) -> dict:
    items, ok = [], 0
    for c in ctx.certs:
        days = c.get("days")
        if days is None:
            items.append(item("cert:%s:unread" % c["id"], "report.cert.unread", "info", False, name=_short(c["name"])))
        elif days < 0:
            items.append(item("cert:%s:expired" % c["id"], "report.cert.expired", "danger", True, name=_short(c["name"]),
                              days=-days))
        elif days < CERT_POINT_DAYS:
            items.append(item("cert:%s:%d" % (c["id"], days // 7), "report.cert.soon", "warn", True, name=_short(c["name"]), days=days))
        elif days < CERT_WARN_DAYS:
            items.append(item("cert:%s:30" % c["id"], "report.cert.month", "info", False, name=_short(c["name"]), days=days))
        else:
            ok += 1
    if not items:
        return section("certificates", "report.sec.certificates", "report.cert.calm", items, n=ok)
    return section("certificates", "report.sec.certificates", "report.cert.summary", items, n=len(ctx.certs), ok=ok)


# ---------------------------------------------------------------------- Lanloom itself
def _older(a: str, b: str) -> bool:
    from ..updates import is_older
    return is_older(a, b)


def _lanloom(ctx: Ctx) -> dict:
    items = []
    upd = ctx.update or {}
    if upd.get("available") and upd.get("latest"):
        items.append(item("upd:%s" % upd["latest"], "report.self.update", "info", True, version=upd["latest"]))
    elif upd.get("held") and upd.get("held_newer"):
        items.append(item("held:%s" % upd["held_newer"], "report.self.held", "info", False, version=upd["held"],
                          newer=upd["held_newer"]))
    st = upd.get("status") or {}
    if st.get("state") in ("failed", "rolled_back") and st.get("version") and ctx.since <= float(st.get("t") or 0) < ctx.until:
        items.append(item("updfail:%s" % (st.get("nonce") or st.get("t")), "report.self.update_failed", "warn", True,
                          version=st["version"], reason_key=st.get("message_key") or "update.err.internal"))
    for ev in ctx.events("app_start"):
        d = ev["data"]
        if d.get("prev") and d.get("version") and _older(d["version"], d["prev"]):
            items.append(item("ver:%s" % d.get("version"), "report.self.rolled_back", "ok", False, at_at=ev["t"],
                              version=d.get("version") or "", was=d.get("prev") or ""))
        elif d.get("prev") and d.get("prev") != d.get("version"):
            items.append(item("ver:%s" % d.get("version"), "report.self.updated", "ok", False, at_at=ev["t"],
                              version=d.get("version") or "", was=d.get("prev") or ""))
        elif d.get("down_s") is not None:
            items.append(item("start:%d" % ev["t"], "report.self.restart", "ok", False, at_at=ev["t"], dur_dur=d.get("down_s") or 0))
        else:
            items.append(item("start:%d" % ev["t"], "report.self.started", "ok", False, at_at=ev["t"]))
    stale = []
    for src in ctx.core.source_list():
        if src["id"] in ("host", "scan", "presence"):
            continue
        if src.get("age_s") is not None and src["age_s"] > STALE_SOURCE_S:
            stale.append(src)
    for src in stale[:6]:
        items.append(item("stale:%s" % src["id"], "report.self.stale", "warn", True, name=_short(src.get("name")),
                          age_dur=src["age_s"]))
    disk = None
    if ctx.data_dir is not None:
        try:
            disk = shutil.disk_usage(str(ctx.data_dir))
        except OSError:
            disk = None
    if disk and (disk.free < DISK_LOW_BYTES or disk.free < disk.total * DISK_LOW_SHARE):
        items.append(item("disk", "report.self.disk_low", "danger" if disk.free < disk.total * 0.03 else "warn", True,
                          free_bytes=disk.free, pct=_pct(disk.free / max(1, disk.total))))
    return section("lanloom", "report.sec.lanloom", "report.self.calm" if not [i for i in items if i["point"]] else "report.self.summary",
                   items, version=(upd.get("current") or ""), free_bytes=disk.free if disk else 0)


for _c in (Contribution("internet", 10, "report.sec.internet", _internet_available, _internet),
           Contribution("devices", 20, "report.sec.devices", _devices_available, _devices),
           Contribution("connections", 30, "report.sec.connections", lambda ctx: bool(_integrations(ctx)), _connections),
           Contribution("logins", 35, "report.sec.logins", lambda ctx: bool(_integrations(ctx)), _logins),
           Contribution("paths", 40, "report.sec.paths", _paths_available, _paths),
           Contribution("controller", 50, "report.sec.controller", _controller_available, _controller),
           Contribution("families", 55, "report.sec.controller", _families_available, _families),
           Contribution("dns", 56, "report.sec.dns", _dns_available, _dns),
           Contribution("certificates", 60, "report.sec.certificates", _certs_available, _certs),
           Contribution("lanloom", 90, "report.sec.lanloom", lambda ctx: True, _lanloom)):
    register(_c)
