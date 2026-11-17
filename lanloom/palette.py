"""Server side of the command palette: one read-only search over what Lanloom knows.

``search(core, query)`` answers ``GET /api/search`` with groups of hits: devices (the network inventory), guests (virtual
machines and containers a hypervisor connection reports), services (which device offers which service), automations and
scripts, and entities of a home-automation hub (the names and states the bridge already filtered for privacy). Views and
commands are matched in the browser - they need no data.

The query is split into words; every word has to match (word start beats part of a word beats a typo). A comparison such
as ``> 25 °C`` or ``<= 40 %`` filters entities by their current numeric value; the rest of the query still narrows by name.
The hub entities are indexed once per snapshot, so a search over thousands of them stays within a few milliseconds.
Nothing here writes anything, and nothing leaves the process.
"""
from __future__ import annotations

import re
import time
import unicodedata

LIMIT_GROUP = 8                      # hits per group in the answer
LIMIT_MAX = 25
MAX_QUERY = 120
TYPO_MIN = 4                         # words shorter than this must match exactly
VALUE_RX = re.compile(r"(?P<op>>=|<=|=>|=<|≥|≤|>|<|=)\s*(?P<num>-?\d+(?:[.,]\d+)?)\s*(?P<unit>[^\s\d][^\s]*)?")
OPS = {">": lambda a, b: a > b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
       "=": lambda a, b: abs(a - b) < 1e-9}
OP_ALIAS = {"=>": ">=", "=<": "<=", "≥": ">=", "≤": "<="}
# units that mean the same: what a user types vs what the hub reports
UNIT_ALIAS = {"c": "c", "°c": "c", "celsius": "c", "grad": "c", "f": "f", "°f": "f", "percent": "%", "prozent": "%", "pct": "%"}
HUB_ONLY_DOMAINS = ("automation", "script")


def fold(text) -> str:
    """Lower-case, without accents, everything that is not a letter or digit becomes one space."""
    s = unicodedata.normalize("NFKD", str(text or "")).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-zß]+", " ", s.replace("ß", "ss")).strip()


def words(text: str) -> list:
    return [w for w in fold(text).split(" ") if w]


def _dist(a: str, b: str, limit: int) -> int:
    """Edit distance with a swap of two neighbours counting as one edit (optimal string alignment), cut off above limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j in range(1, len(b) + 1):
            c = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + c)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            best = min(best, v)
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def typo_limit(token: str) -> int:
    n = len(token)
    return 0 if n < TYPO_MIN else (1 if n < 8 else 2)


def near_words(token: str, vocab: dict, lim: int) -> dict:
    """{word: distance} of the words in vocab ({word: set of its letters}) within lim edits of token (or of its beginning)."""
    ts, out = set(token), {}
    for w, ws in vocab.items():
        if len(ts - ws) > lim:                      # too many letters missing altogether: no need to count edits
            continue
        cut = w[:len(token) + 1] if len(w) > len(token) else w      # a typo inside the beginning of a longer word
        d = min(_dist(token, cut, lim), _dist(token, w, lim))
        if d <= lim:
            out[w] = d
    return out


def word_score(token: str, haystack: str, hay_words: list, typos: bool, near: dict | None = None) -> int:
    """How well one query word matches a text: 100 whole word, 80 start of a word, 55 inside a word, 25-45 typo, 0 none.
    ``near`` ({word: distance}, see near_words) saves counting edits again for every text of a big list."""
    if token in hay_words:
        return 100
    for w in hay_words:
        if w.startswith(token):
            return 80
    if len(token) >= 2 and token in haystack:
        return 55
    if typos:
        lim = typo_limit(token)
        if lim:
            best = lim + 1
            if near is not None:
                for w in hay_words:
                    d = near.get(w)
                    if d is not None and d < best:
                        best = d
            else:
                for w in hay_words:
                    cut = w[:len(token) + 1] if len(w) > len(token) else w
                    best = min(best, _dist(token, cut, lim), _dist(token, w, lim))
                    if best == 0:
                        break
            if best <= lim:
                return 45 - 10 * (best - 1)
    return 0


class Entry:
    """One searchable thing: a title (what the user reads) and further words (address, id, kind ...)."""
    __slots__ = ("title", "extra", "t_words", "x_words", "t_text", "x_text", "hit", "all_text", "all_words")

    def __init__(self, title, extra, hit):
        self.title, self.extra, self.hit = title, extra, hit
        self.t_text = fold(title)
        self.x_text = fold(extra)
        self.t_words = self.t_text.split(" ") if self.t_text else []
        self.x_words = self.x_text.split(" ") if self.x_text else []
        self.all_text = self.t_text + " " + self.x_text          # quick test: a word that is not inside here matches nothing
        self.all_words = frozenset(self.t_words + self.x_words)


def score(tokens: list, e: Entry, typos: bool, near: dict | None = None) -> int:
    """Average of the best score per query word (title counts fully, other words at 80 %); 0 when a word matches nothing."""
    if not tokens:
        return 1
    total = 0
    for tok in tokens:
        nt = near.get(tok) if near else None
        if tok not in e.all_text:                              # not inside the text: only a typo can still match
            if not typos or not typo_limit(tok) or (nt is not None and nt.keys().isdisjoint(e.all_words)):
                return 0
        t = word_score(tok, e.t_text, e.t_words, typos, nt)
        # the other words count at 80 %: they cannot beat a start of a title word, so look only when the title is weaker
        x = int(0.8 * word_score(tok, e.x_text, e.x_words, typos, nt)) if t < 80 else 0
        s = max(t, x)
        if not s:
            return 0
        total += s
    return total // len(tokens)


def rank(entries: list, tokens: list, limit: int, cache: dict | None = None, universe: list | None = None) -> list:
    """The hits of the best-matching entries. Typo tolerance only when nothing matches exactly; ``cache`` keeps the vocabulary of
    ``universe`` (all entries the list is drawn from) and the near words per query word, so a big list is searched fast."""
    out = []
    for typos in (False, True):
        near = None
        if typos:
            cache = {} if cache is None else cache
            if "vocab" not in cache:
                cache["vocab"] = {w: set(w) for e in (universe or entries) for w in e.t_words + e.x_words}
            memo = cache.setdefault("near", {})
            near = {}
            for tok in tokens:
                lim = typo_limit(tok)
                if lim:
                    if tok not in memo:
                        memo[tok] = near_words(tok, cache["vocab"], lim)
                    near[tok] = memo[tok]
        out = []
        for e in entries:
            s = score(tokens, e, typos, near)
            if s:
                out.append((s, e))
        if out or not tokens:
            break
    out.sort(key=lambda x: (-x[0], len(x[1].title), x[1].title))
    return [e.hit for _s, e in out[:limit]]


# ------------------------------------------------------------------ query: words and a value comparison
def parse(query: str) -> dict:
    """{'text': words without the comparison, 'op': '>' ..., 'num': float, 'unit': 'c' ...} - op None without a comparison."""
    q = str(query or "").strip()[:MAX_QUERY]
    m = VALUE_RX.search(q)
    if not m:
        return {"text": q, "op": None, "num": None, "unit": ""}
    op = OP_ALIAS.get(m.group("op"), m.group("op"))
    unit = (m.group("unit") or "").strip().casefold()
    return {"text": (q[:m.start()] + " " + q[m.end():]).strip(), "op": op, "num": float(m.group("num").replace(",", ".")),
            "unit": UNIT_ALIAS.get(unit, unit)}


def _number(state):
    if isinstance(state, bool) or state is None:
        return None
    if isinstance(state, (int, float)):
        return float(state)
    try:
        return float(str(state).strip().replace(",", "."))
    except ValueError:
        return None


def _unit_ok(want: str, have) -> bool:
    if not want:
        return True
    h = str(have or "").strip().casefold()
    return UNIT_ALIAS.get(h, h) == want


# ------------------------------------------------------------------ the groups
_device_cache: dict = {}            # (core, device id) -> (signature, device entry, service entries): folding names is the cost, not matching


def _device_entries(core, devices) -> tuple:
    """(device entries, service entries) of the visible devices; an entry is built again only when what is searched changed."""
    from . import classify
    key = id(core)
    dev_out, svc_out, seen = [], [], set()
    for d in devices:
        if d.get("hidden"):
            continue
        did = d["id"]
        seen.add((key, did))
        ports = tuple((p.get("port"), p.get("service")) for p in d.get("ports") or [])
        sig = (d.get("title"), d.get("ip"), d.get("mac"), d.get("kind"), tuple(d.get("hostnames") or ()), d.get("vendor"), d.get("model"),
               ports, tuple(d.get("services") or ()), bool(d.get("is_self")))
        got = _device_cache.get((key, did))
        if got is None or got[0] != sig:
            title = d.get("title") or ""
            extra = " ".join([d.get("ip") or "", d.get("mac") or "", d.get("kind") or "", " ".join(d.get("hostnames") or []),
                              d.get("vendor") or "", d.get("model") or ""])
            dev = Entry(title, extra, {"kind": "device", "id": did, "title": title, "ip": d.get("ip"), "device_kind": d.get("kind"),
                                       "status": None, "self": bool(d.get("is_self"))})
            by_service = {}
            for port, name in ports:
                if name and name not in by_service:
                    by_service[name] = port
            svcs = []
            for name, port in by_service.items():
                label = (classify.services_of({"ports": [{"service": name}]}) or [name])[0]
                svcs.append(Entry(label, " ".join([name, title, d.get("ip") or "", " ".join(d.get("services") or [])]),
                                  {"kind": "service", "id": "%s|%s" % (did, name), "title": label, "device": did, "device_title": title,
                                   "device_kind": d.get("kind"), "ip": d.get("ip"), "port": port, "status": None}))
            got = _device_cache[(key, did)] = (sig, dev, svcs)
        _sig, dev, svcs = got
        dev.hit["status"] = d.get("status")                      # the one thing that moves without the entry changing
        dev_out.append(dev)
        for e in svcs:
            e.hit["status"] = d.get("status")
        svc_out.extend(svcs)
    if len(_device_cache) > 4 * max(len(devices), 64) + 1024:    # forget devices that are gone (and other cores of a test run)
        for k in [k for k in _device_cache if k[0] != key or k not in seen]:
            _device_cache.pop(k, None)
    return dev_out, svc_out


def _guest_entries(core, devices) -> list:
    out = []
    by_ip = {d["ip"]: d for d in devices if d.get("ip")}
    for e in core.live_configs():
        st = e.get("state") or {}
        if st.get("guests") is None:
            continue
        hosts = {h["id"]: h for h in st.get("hosts") or []}
        for g in st["guests"]:
            ips = [i for i in g.get("ips") or [] if isinstance(i, str)]
            dev = next((by_ip[i] for i in ips if i in by_ip), None)
            host = hosts.get(g.get("host")) or {}
            name = g.get("name") or str(g.get("vmid") or g.get("id"))
            out.append(Entry(name, " ".join(ips + [str(g.get("vmid") or ""), str(g.get("type") or ""), host.get("name") or "", e.get("name") or ""]),
                             {"kind": "guest", "id": "%s|%s" % (e.get("id"), g.get("id")), "title": name, "vmid": g.get("vmid"),
                              "guest_type": g.get("type"), "host": host.get("name"), "status": g.get("status"),
                              "template": bool(g.get("template")), "ip": ips[0] if ips else None,
                              "device": dev["id"] if dev else None}))
    return out


class HubIndex:
    """The entities of one hub snapshot, normalised once; states are read fresh from the entry on every search."""

    def __init__(self, integ_id, snapshot_key, entry, snap):
        self.integ_id, self.key = integ_id, snapshot_key
        areas = {a.get("id"): a.get("name") for a in snap.get("areas") or []}
        self.entries, self.automations = [], []
        self.cache_e, self.cache_a = {}, {}          # typo vocabularies (see rank)
        for ent in snap.get("entities") or []:
            eid = ent.get("entity_id")
            if not isinstance(eid, str):
                continue
            domain = eid.split(".", 1)[0]
            name = ent.get("name") or eid
            area = areas.get(ent.get("area")) or ent.get("area") or ""
            hit = {"kind": "automation" if domain in HUB_ONLY_DOMAINS else "entity", "id": eid, "title": name, "entity_id": eid, "domain": domain,
                   "area": area, "unit": ent.get("unit"), "integ": integ_id}
            (self.automations if domain in HUB_ONLY_DOMAINS else self.entries).append(Entry(name, "%s %s %s" % (eid, area, domain), hit))


_index_cache: dict = {}


def _hub_index(core, integ_id, entry):
    snap = entry.get("snapshot") or {}
    key = (id(snap), entry.get("t"))
    got = _index_cache.get((id(core), integ_id))
    if got is None or got.key != key:
        got = HubIndex(integ_id, key, entry, snap)
        _index_cache[(id(core), integ_id)] = got
        if len(_index_cache) > 64:
            _index_cache.pop(next(iter(_index_cache)))
    return got


def _hub_hits(core, tokens: list, value: dict, limit: int) -> tuple:
    entities, automations = [], []
    for integ_id in core.hub_ids():
        entry = core.hub.get(integ_id)
        if not entry or not entry.get("snapshot"):
            continue
        idx = _hub_index(core, integ_id, entry)
        states = entry.get("states") or {}
        comp = _hub_component(core, integ_id)
        # states: the live value wins, the snapshot value is the fallback
        snap_states = {x.get("entity_id"): x.get("state") for x in entry["snapshot"].get("entities") or []}

        def live_state(eid):
            got = states.get(eid)
            return got[0] if got else snap_states.get(eid)

        pool = idx.entries
        if value["op"]:
            test = OPS[value["op"]]
            keep = []
            for e in pool:
                n = _number(live_state(e.hit["id"]))
                if n is not None and test(n, value["num"]) and _unit_ok(value["unit"], e.hit.get("unit")):
                    keep.append(e)
            pool = keep
        for hit in rank(pool, tokens, len(pool) if value["op"] else limit, idx.cache_e, idx.entries):
            entities.append(dict(hit, state=live_state(hit["id"]), comp=comp))
        if not value["op"]:
            for hit in rank(idx.automations, tokens, limit, idx.cache_a):
                automations.append(dict(hit, state=live_state(hit["id"]), comp=comp))
    if value["op"]:                          # a comparison lists the most extreme values first (highest for > and >=, lowest for < and <=)
        sign = -1 if value["op"] in (">", ">=") else 1
        entities.sort(key=lambda h: sign * (_number(h["state"]) or 0))
    return entities[:limit], automations[:limit]


def _hub_component(core, integ_id):
    from . import mapmodel
    try:
        return mapmodel._special_components(core, mapmodel._visible_devices(core))["hub"].get(integ_id)
    except Exception:                        # the card of the hub is a convenience: a search must not fail without it
        return None


def search(core, query: str, limit: int = LIMIT_GROUP) -> dict:
    """{'q', 'value': {op, num, unit} | None, 'groups': {devices, guests, services, automations, entities}, 'ms'}."""
    t0 = time.perf_counter()
    limit = max(1, min(LIMIT_MAX, int(limit or LIMIT_GROUP)))
    value = parse(query)
    tokens = words(value["text"])
    groups = {"devices": [], "guests": [], "services": [], "automations": [], "entities": []}
    if tokens or value["op"]:
        if not value["op"]:                  # a value comparison asks about hub entities only
            devices = core.devices()
            dev_entries, svc_entries = _device_entries(core, devices)
            groups["devices"] = rank(dev_entries, tokens, limit)
            groups["guests"] = rank(_guest_entries(core, devices), tokens, limit)
            groups["services"] = rank(svc_entries, tokens, limit)
        groups["entities"], groups["automations"] = _hub_hits(core, tokens, value, limit)
    return {"q": str(query or "")[:MAX_QUERY], "value": {k: value[k] for k in ("op", "num", "unit")} if value["op"] else None,
            "groups": groups, "ms": round((time.perf_counter() - t0) * 1000, 1)}
