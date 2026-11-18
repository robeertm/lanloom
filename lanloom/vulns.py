"""Vulnerability hints from an offline feed. Never asks anything on the network.

The image ships ``lanloom/data/vulnfeed.json.gz``, built by ``scripts/build_vulnfeed.py`` from public catalogs (the
catalog of known exploited vulnerabilities and the machine-readable affected-version ranges of the national
vulnerability database). It is renewed with every Lanloom release and never downloaded at run time. An administrator
may import a newer feed file of the same format (``<data>/vulns/feed.json.gz``); its entries are added, an entry with
the same id replaces the bundled one, and removing the import goes back to the bundled feed.

Feed format (``format`` 1)::

    {"format": 1, "as_of": "YYYY-MM-DD", "sources": ["..."],
     "entries": [{"id": "CVE-2024-12345", "sev": "critical", "score": 9.8, "kev": "2024-05-01", "ransom": false,
                  "title": "...", "action": "update" | "replace",
                  "match": [{"v": "<maker token>", "p": "<model token>", "r": [{"lt": "1.2.3"}, ...]}]}]}

``v`` and ``p`` are the maker and model in normalised form (lower case, letters and digits only, ``+`` written as
``plus``); ``r`` lists the affected firmware ranges (``ge``/``gt`` lower bound, ``lt``/``le`` upper bound, or ``eq`` for
one version); an empty list means every version is affected.

Matching is deliberately strict - better no hint than a wrong one:

* the device's **maker and model** must be known (from the device itself, a connected family or the hub; a maker
  known only from the hardware address register lowers the result to "possible"), the model must equal the feed's
  model exactly (no partial matches), and the maker must be the feed's maker;
* the **firmware** must be known and comparable with the range (same shape: ``5.21(aazf.13)c0`` is compared with
  ``5.21(aazf.10)c0``, never with ``5.21(aasz.3)c0``). Firmware typed in by a person, reported by a connected family,
  the hub or the device's management protocol gives a **certain** result; firmware read from an announcement gives a
  **possible** one; an entry for every version is always **possible**;
* a device whose maker, model or firmware is unknown or unreadable is listed as *not checked*, with the reason.

The result is a hint about published information, never a test of the device itself.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import eol as eol_mod

log = logging.getLogger("lanloom")

DATA_FILE = Path(__file__).resolve().parent / "data" / "vulnfeed.json.gz"
FORMAT = 1
MAX_ENTRIES = 50_000
MAX_MATCHES = 64
MAX_RANGES = 16
MAX_IMPORT_BYTES = 8 * 1024 * 1024          # compressed upload
MAX_PLAIN_BYTES = 64 * 1024 * 1024          # after decompression
SEVERITIES = ("critical", "high", "medium", "low", "unknown")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
ACTIONS = ("update", "replace")
CERTAINTIES = ("certain", "possible")
STRONG_FW = ("own", "family", "hub", "snmp", "agent")      # typed in by a person or read from a management interface
MEDIUM_FW = ("upnp", "mdns")                               # read from what the device announces
WEAK_VENDOR = ("oui", "name")                              # the hardware address register (who registered the chip)
UNCHECKED = ("firmware_unknown", "firmware_unreadable", "model_unknown", "vendor_unknown")   # what a person can fix first
CACHE_S = 20.0
ID_RX = re.compile(r"[A-Z]{2,12}-\d{4}-\d{1,9}")
TOKEN_RX = re.compile(r"[a-z0-9]{1,60}")
CTRL_RX = re.compile(r"[\x00-\x1f\x7f]")
RANGE_KEYS = ("ge", "gt", "le", "lt", "eq")
DISMISS_NOTE = 200


# ---------------------------------------------------------------------- normalising
def norm_token(value) -> str:
    """Maker or model in the feed's form: lower case, ``+`` as ``plus``, letters and digits only."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower().replace("+", "plus"))


def vendor_matches(feed_vendor: str, device_vendor) -> bool:
    """The feed's maker token equals the first one to four words of the device's maker, joined (``ex link
    technologies`` -> ``exlink``). Never a substring match: ``hp`` does not match ``hpe``."""
    if not feed_vendor or not isinstance(device_vendor, str):
        return False
    words = [w for w in re.split(r"[^a-z0-9+]+", device_vendor.lower().replace("+", "plus")) if w]
    joined = ""
    for w in words[:4]:
        joined += re.sub(r"[^a-z0-9]", "", w)
        if joined == feed_vendor:
            return True
        if len(joined) >= len(feed_vendor):
            break
    return False


def model_keys(model, vendor) -> set:
    """The device's model in the feed's form, also without a leading maker name (``Maker NAS-326`` -> ``nas326``)."""
    m = norm_token(model)
    out = set()
    if len(m) >= 3 and re.search(r"\d", m):           # "Router", "Gateway": too general to stand for a model
        out.add(m)
    if isinstance(vendor, str):
        first = norm_token((vendor.split() or [""])[0])
        if first and m.startswith(first) and len(m) - len(first) >= 3 and re.search(r"\d", m[len(first):]):
            out.add(m[len(first):])
    return out


_VERSION_START_RX = re.compile(r"\d+(?:[._]\d+)+")


def version_core(value) -> Optional[str]:
    """The comparable part of a firmware text: from the first dotted number to the first blank
    (``V5.21(AAZF.13)C0`` -> ``5.21(aazf.13)c0``, ``R7-V1.0.11.116_10.2.100`` -> ``1.0.11.116_10.2.100``)."""
    if not isinstance(value, str):
        return None
    s = value.strip().lower().replace("\\", "")
    if not s:
        return None
    m = _VERSION_START_RX.search(s)
    if m:
        core = s[m.start():].split()[0]
    else:
        bare = re.fullmatch(r"v?(\d{1,9})", s)
        if not bare:
            return None
        core = bare.group(1)
    return core[:48]


def _parts(core: str) -> list:
    return [int(p) if p.isdigit() else p for p in re.findall(r"\d+|[a-z]+", core)]


def compare(a, b) -> Optional[int]:
    """-1 / 0 / 1, or None when the two versions do not have the same shape (then nothing is claimed)."""
    ca, cb = version_core(a), version_core(b)
    if ca is None or cb is None:
        return None
    pa, pb = _parts(ca), _parts(cb)
    for i in range(max(len(pa), len(pb))):
        x = pa[i] if i < len(pa) else None
        y = pb[i] if i < len(pb) else None
        if x is None or y is None:
            rest = (pb if x is None else pa)[i:]
            if all(isinstance(r, int) and r == 0 for r in rest):
                return 0
            if isinstance(rest[0], int):
                return -1 if x is None else 1
            return None                                   # "1.0" against "1.0rc1": a pre-release, say nothing
        if isinstance(x, int) and isinstance(y, int):
            if x != y:
                return -1 if x < y else 1
        elif isinstance(x, str) and isinstance(y, str):
            if x != y:
                return None                               # another branch ("aazf" against "aasz")
        else:
            return None
    return 0


def in_range(version, rng: dict) -> Optional[bool]:
    """Is ``version`` inside the range? None when it cannot be compared with a bound."""
    if "eq" in rng:
        c = compare(version, rng["eq"])
        return None if c is None else c == 0
    for key, ok in (("ge", (0, 1)), ("gt", (1,)), ("le", (-1, 0)), ("lt", (-1,))):
        if key in rng:
            c = compare(version, rng[key])
            if c is None:
                return None
            if c not in ok:
                return False
    return True


# ---------------------------------------------------------------------- the feed
def _text(value, limit: int) -> str:
    return re.sub(r"\s+", " ", CTRL_RX.sub(" ", value if isinstance(value, str) else "")).strip()[:limit]


def clean_entry(raw) -> Optional[dict]:
    """One validated feed entry or None."""
    if not isinstance(raw, dict):
        return None
    vid = raw.get("id")
    if not isinstance(vid, str) or not ID_RX.fullmatch(vid):
        return None
    sev = raw.get("sev") if raw.get("sev") in SEVERITIES else "unknown"
    score = raw.get("score")
    score = round(float(score), 1) if isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= score <= 10 else None
    kev = raw.get("kev") if eol_mod.parse_date(raw.get("kev")) else ""
    action = raw.get("action") if raw.get("action") in ACTIONS else "update"
    matches = []
    for m in (raw.get("match") or [])[:MAX_MATCHES] if isinstance(raw.get("match"), list) else []:
        if not isinstance(m, dict):
            return None
        v, p, r = m.get("v"), m.get("p"), m.get("r", [])
        if not (isinstance(v, str) and TOKEN_RX.fullmatch(v) and isinstance(p, str) and TOKEN_RX.fullmatch(p) and len(p) >= 3):
            return None
        if not isinstance(r, list) or len(r) > MAX_RANGES:
            return None
        ranges = []
        for rng in r:
            if not isinstance(rng, dict) or not rng or any(k not in RANGE_KEYS for k in rng):
                return None
            if "eq" in rng and len(rng) > 1:
                return None
            if not all(isinstance(x, str) and 0 < len(x) <= 48 and version_core(x) for x in rng.values()):
                return None
            ranges.append({k: rng[k] for k in RANGE_KEYS if k in rng})
        matches.append({"v": v, "p": p, "r": ranges})
    if not matches:
        return None
    return {"id": vid, "sev": sev, "score": score, "kev": kev, "ransom": raw.get("ransom") is True,
            "title": _text(raw.get("title"), 200), "action": action, "match": matches}


def _decompress(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(bytes(data))) as gz:
                data = gz.read(MAX_PLAIN_BYTES + 1)
        except (OSError, EOFError):
            raise ValueError("vulns.err.feed")
        if len(data) > MAX_PLAIN_BYTES:
            raise ValueError("vulns.err.too_large")
    return bytes(data)


def _header(doc) -> dict:
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or not eol_mod.parse_date(doc.get("as_of")):
        raise ValueError("vulns.err.feed")
    return {"as_of": doc["as_of"], "sources": [_text(x, 60) for x in (doc.get("sources") or [])[:8] if isinstance(x, str)]}


def iter_raw(lines) -> tuple:
    """(header, iterator of raw entries) from the lines of a feed file. Two layouts are read: one JSON document per line
    (a header line, then one entry per line - what the build script writes, read line by line with little memory), or a
    single JSON document with an ``entries`` list."""
    it = iter(lines)
    first = next(it, b"")
    try:
        doc = json.loads(first)
    except (UnicodeDecodeError, ValueError):
        doc = None
    if isinstance(doc, dict) and "entries" not in doc:
        head = _header(doc)

        def rows():
            for line in it:
                if line.strip():
                    try:
                        yield json.loads(line)
                    except (UnicodeDecodeError, ValueError):
                        yield None
        return head, rows()
    rest = first + b"".join(it) if isinstance(first, bytes) else first + "".join(it)
    try:
        doc = json.loads(rest)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("vulns.err.feed")
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        raise ValueError("vulns.err.feed")
    return _header(doc), iter(doc["entries"])


def _clean_all(raws, keep: Optional[set] = None) -> tuple:
    out, seen, skipped = [], set(), 0
    for raw in raws:
        if len(seen) >= MAX_ENTRIES:
            break
        e = clean_entry(raw)
        if e is None:
            skipped += 1
            continue
        if e["id"] in seen or (keep is not None and e["id"] in keep):
            continue
        seen.add(e["id"])
        out.append(e)
    return out, skipped


def parse_feed(data: bytes) -> tuple:
    """(meta, [entries]) from the bytes of a feed file (gzip or plain, either layout). Raises ValueError("vulns.err.feed")."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("vulns.err.feed")
    head, raws = iter_raw(_decompress(data).splitlines())
    out, skipped = _clean_all(raws)
    return dict(head, entries=len(out), skipped=skipped), out


def write_feed(path: Path, meta: dict, entries: list, note: str = "") -> None:
    """The file layout the app reads with little memory: a header line, then one entry per line (gzip, no time stamp)."""
    head = {"format": FORMAT, "as_of": meta["as_of"], "sources": meta.get("sources") or []}
    if note:
        head["note"] = note
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0, filename="") as gz:
        gz.write((json.dumps(head, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
        for e in entries:
            gz.write((json.dumps(e, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(path)


def _stream_file(path: Path, keep: Optional[set] = None) -> tuple:
    """(meta, entries) of a feed file, read line by line; ``(None, [])`` for a missing or broken one (never breaks the start)."""
    try:
        with gzip.open(path, "rb") as fh:
            head, raws = iter_raw(fh)
            out, skipped = _clean_all(raws, keep)
    except (OSError, EOFError, ValueError):
        try:
            meta, out = parse_feed(Path(path).read_bytes())           # a plain (not compressed) file
            out = [e for e in out if keep is None or e["id"] not in keep]
            return meta, out
        except (OSError, ValueError):
            if Path(path).exists():
                log.warning("vulnerability feed %s is not readable, ignored", Path(path).name)
            return None, []
    return dict(head, entries=len(out), skipped=skipped), out


def _stream_into(path: Path, feed) -> Optional[dict]:
    """Add the entries of a feed file to ``feed`` line by line (entries the feed already has are kept). Returns the file's
    meta, or None for a missing or broken file (never breaks the start)."""
    added = skipped = 0
    try:
        with gzip.open(path, "rb") as fh:
            head, raws = iter_raw(fh)
            for raw in raws:
                if added >= MAX_ENTRIES:
                    break
                e = clean_entry(raw)
                if e is None:
                    skipped += 1
                elif feed.add(e):
                    added += 1
            return dict(head, entries=added, skipped=skipped)
    except (OSError, EOFError, ValueError):
        meta, out = _stream_file(path)                       # a plain (not compressed) file, or a broken one
        if meta is None:
            return None
        n = sum(1 for e in out if feed.add(e))
        return dict(meta, entries=n)


def load_file(path: Path) -> tuple:
    return _stream_file(path)


_SEP_ROW, _SEP_FIELD = "\x1e", "\x1f"


class Feed:
    """The entries in memory, compact: a row per entry and, per model, one string with the maker and ranges of every entry
    that names it (taken apart only when a device of that model is checked). About 4 MB for the bundled feed, built while
    the file is read line by line."""

    def __init__(self, entries=(), as_of: Optional[str] = None, sources: Optional[list] = None, imported: Optional[dict] = None):
        self.as_of = as_of
        self.sources = list(sources or [])
        self.imported = imported
        self._rows: dict = {}
        self._building: dict = {}
        self._ranges: dict = {}
        self.exploited = 0
        for e in entries:
            self.add(e)
        self.finish()

    def add(self, e: dict) -> bool:
        if e["id"] in self._rows:
            return False
        self._rows[e["id"]] = (e["sev"], e["score"], e["kev"], e["ransom"], e["title"], e["action"])
        self.exploited += 1 if e["kev"] else 0
        for m in e["match"]:
            r = json.dumps(m["r"], separators=(",", ":"))
            r = self._ranges.setdefault(r, r)
            self._building.setdefault(m["p"], []).append(_SEP_FIELD.join((e["id"], m["v"], r)))
        return True

    def finish(self) -> "Feed":
        built = {k: _SEP_ROW.join(v) for k, v in self._building.items()}
        if hasattr(self, "_index"):
            for k, v in built.items():
                self._index[k] = self._index[k] + _SEP_ROW + v if k in self._index else v
        else:
            self._index = built
        self._building, self._ranges = {}, {}
        return self

    @classmethod
    def from_raw(cls, raw_entries: list, as_of: str, sources: Optional[list] = None) -> "Feed":
        return cls([e for e in (clean_entry(r) for r in raw_entries) if e], as_of, sources)

    @classmethod
    def load(cls, extra: Optional[Path] = None, bundled: Path = DATA_FILE) -> "Feed":
        feed = cls()
        imeta = None
        if extra is not None and Path(extra).is_file():
            imeta = _stream_into(Path(extra), feed)           # an imported entry replaces the bundled one with the same id
        meta = _stream_into(bundled, feed)
        feed.finish()
        feed.as_of = (meta or {}).get("as_of")
        feed.sources = (meta or {}).get("sources") or []
        if imeta and imeta["entries"]:
            feed.imported = {"as_of": imeta["as_of"], "entries": imeta["entries"], "sources": imeta["sources"], "at": Path(extra).stat().st_mtime}
            if not feed.as_of or imeta["as_of"] > feed.as_of:
                feed.as_of = imeta["as_of"]
        return feed

    def __len__(self) -> int:
        return len(self._rows)

    def entry(self, vid: str) -> Optional[dict]:
        row = self._rows.get(vid)
        if row is None:
            return None
        sev, score, kev, ransom, title, action = row
        return {"id": vid, "sev": sev, "score": score, "kev": kev, "ransom": ransom, "title": title, "action": action}

    @property
    def entries(self) -> dict:
        return {vid: self.entry(vid) for vid in self._rows}

    def has(self, model_key: str) -> bool:
        return model_key in self._index

    def lookup(self, model_key: str):
        """(entry, {"v", "p", "r"}) of every entry naming this model."""
        packed = self._index.get(model_key)
        if not packed:
            return
        for part in packed.split(_SEP_ROW):
            vid, v, r = part.split(_SEP_FIELD, 2)
            e = self.entry(vid)
            if e is not None:
                yield e, {"v": v, "p": model_key, "r": json.loads(r)}


# ---------------------------------------------------------------------- matching one device
def match(candidates: list, feed: Feed) -> dict:
    """Findings for one device from its identity candidates.

    A candidate is ``{"vendor", "vendor_src", "model", "model_src", "firmware", "fw_src"}``. Returns
    ``{"findings": [...], "status": "found"|"not_affected"|"none"|"unchecked", "reason": key or None}``."""
    findings: dict = {}
    reasons_unchecked = []
    looked = False
    any_vendor = any(c.get("vendor") for c in candidates)
    any_model = any(c.get("model") and c.get("model_src") not in WEAK_VENDOR for c in candidates)
    for c in candidates:
        vendor, model = c.get("vendor"), c.get("model")
        if not vendor or not model or c.get("model_src") in WEAK_VENDOR:
            continue
        keys = model_keys(model, vendor)
        if not keys:
            continue
        looked = True
        weak_vendor = c.get("vendor_src") in WEAK_VENDOR
        fw, fw_src = c.get("firmware"), c.get("fw_src")
        for key in keys:
            for entry, m in feed.lookup(key):
                if not vendor_matches(m["v"], vendor):
                    continue
                why = ["maker_registry" if weak_vendor else "maker", "model"]
                fixed = None
                if not m["r"]:
                    certainty = "possible"
                    why.append("all_versions")
                else:
                    if not fw:
                        reasons_unchecked.append("firmware_unknown")
                        continue
                    hit, unreadable = None, False
                    for rng in m["r"]:
                        res = in_range(fw, rng)
                        if res is None:
                            unreadable = True
                        elif res:
                            hit = rng
                            break
                    if hit is None:
                        if unreadable:
                            reasons_unchecked.append("firmware_unreadable")
                        continue
                    fixed = hit.get("lt")
                    why.append("firmware_in_range")
                    if fw_src in STRONG_FW and not weak_vendor:
                        certainty = "certain"
                    else:
                        certainty = "possible"
                        if fw_src not in STRONG_FW:
                            why.append("firmware_announced")
                    why.append("firmware_src_" + (fw_src if fw_src in STRONG_FW + MEDIUM_FW else "other"))
                prev = findings.get(entry["id"])
                if prev and (prev["certainty"] == "certain" or certainty == "possible"):
                    continue
                findings[entry["id"]] = {"entry": entry, "certainty": certainty, "reasons": why, "fixed": fixed,
                                         "firmware": fw if m["r"] else (fw or None), "firmware_source": fw_src if fw else None,
                                         "model": model, "vendor": vendor}
    if findings:
        return {"findings": list(findings.values()), "status": "found", "reason": None}
    if not any_vendor:
        return {"findings": [], "status": "unchecked", "reason": "vendor_unknown"}
    if not any_model or not looked:
        return {"findings": [], "status": "unchecked", "reason": "model_unknown"}
    if reasons_unchecked:
        reason = "firmware_unreadable" if "firmware_unreadable" in reasons_unchecked else "firmware_unknown"
        return {"findings": [], "status": "unchecked", "reason": reason}
    listed = any(feed.has(k) for c in candidates for k in model_keys(c.get("model"), c.get("vendor"))
                 if c.get("vendor") and c.get("model_src") not in WEAK_VENDOR)
    return {"findings": [], "status": "not_affected" if listed else "none", "reason": None}


# ---------------------------------------------------------------------- the application part
class Vulns:
    """Vulnerability hints of all devices (``core.vulns``)."""

    def __init__(self, core, clock: Callable[[], float] = time.time):
        self.core = core
        self.store = core.store
        self.clock = clock
        data_dir = getattr(core.cfg, "data_dir", None)
        self.extra = (Path(data_dir) / "vulns" / "feed.json.gz") if data_dir else None
        self._feed: Optional[Feed] = None   # loaded on first use (a few seconds on a small host; not at start)
        self._feed_lock = threading.Lock()
        self._lock = threading.Lock()
        self._cache = None              # (stamp, result)

    @property
    def feed(self) -> Feed:
        if self._feed is None:
            with self._feed_lock:
                if self._feed is None:
                    self._feed = Feed.load(self.extra)
        return self._feed

    @feed.setter
    def feed(self, value: Feed) -> None:
        self._feed = value

    def table(self) -> Feed:
        """The feed in use: the bundled (plus imported) one, or the demo's fictional one."""
        f = getattr(getattr(self.core, "demo", None), "vulnfeed", None)
        return f if isinstance(f, Feed) else self.feed

    def invalidate(self) -> None:
        self._cache = None

    # ------------------------------------------------------------------ feed info and import
    def feed_info(self) -> dict:
        f = self.table()
        return {"as_of": f.as_of, "entries": len(f), "exploited": f.exploited, "sources": f.sources, "imported": f.imported,
                "demo": f is not self.feed}

    def import_feed(self, data: bytes) -> dict:
        if self.extra is None:
            raise ValueError("vulns.err.no_data_dir")
        if len(data) > MAX_IMPORT_BYTES:
            raise ValueError("vulns.err.too_large")
        meta, entries = parse_feed(data)
        if not entries:
            raise ValueError("vulns.err.empty")
        self.extra.parent.mkdir(parents=True, exist_ok=True)
        write_feed(self.extra, meta, entries)
        self.feed = Feed.load(self.extra)
        self.invalidate()
        return dict(meta, info=self.feed_info())

    def remove_import(self) -> dict:
        if self.extra is not None and self.extra.is_file():
            self.extra.unlink()
        self.feed = Feed.load(self.extra)
        self.invalidate()
        return self.feed_info()

    # ------------------------------------------------------------------ dismissing
    def _dismissed(self) -> dict:
        d = self.store.get_setting("vulns_dismissed", {}) or {}
        return d if isinstance(d, dict) else {}

    def dismiss(self, device_id: str, vid: str, note: str = "", by: str = "") -> dict:
        if not isinstance(vid, str) or not ID_RX.fullmatch(vid) or not self.store.device(device_id):
            raise ValueError("vulns.err.unknown")
        d = self._dismissed()
        d["%s|%s" % (device_id, vid)] = {"at": self.clock(), "by": _text(by, 48), "note": _text(note, DISMISS_NOTE)}
        self.store.set_setting("vulns_dismissed", d)
        self.invalidate()
        return self.device(device_id)

    def restore(self, device_id: str, vid: str) -> dict:
        d = self._dismissed()
        if d.pop("%s|%s" % (device_id, vid), None) is None:
            raise ValueError("vulns.err.unknown")
        self.store.set_setting("vulns_dismissed", d)
        self.invalidate()
        return self.device(device_id)

    def forget_device(self, device_id: str) -> None:
        d = self._dismissed()
        keep = {k: v for k, v in d.items() if not k.startswith(device_id + "|")}
        if keep != d:
            self.store.set_setting("vulns_dismissed", keep)
        self.invalidate()

    # ------------------------------------------------------------------ facts about the devices
    def _hub_facts(self) -> dict:
        """Maker, model and firmware the hub's device registry holds, by device id."""
        out = {}
        try:
            by_net = (self.core.hub_merge() or {}).get("by_net") or {}
        except Exception:
            return out
        for did, matches in by_net.items():
            for m in matches:
                snap = (self.core.hub.get(m.get("integ")) or {}).get("snapshot") or {}
                for d in snap.get("devices") or []:
                    if d.get("id") == m.get("device_id") and (d.get("model") or d.get("sw")):
                        out.setdefault(did, (d.get("manufacturer"), d.get("model"), d.get("sw")))
        return out

    def _family_models(self) -> dict:
        out = {}
        try:
            entries = self.core.live_configs()
        except Exception:
            entries = []
        for entry in entries:
            for eq in ((entry.get("state") or {}).get("equipment")) or []:
                ip = eq.get("mgmt_ip")
                did = self.core.ip_to_device.get(ip) if ip else None
                if did and eq.get("model"):
                    out.setdefault(did, (eq.get("vendor"), eq.get("model"), eq.get("firmware")))
        return out

    @staticmethod
    def candidates(view: dict, inv: Optional[dict], hub: Optional[tuple] = None, family: Optional[tuple] = None) -> list:
        """Every combination of maker, model and firmware Lanloom knows for one device, strongest first."""
        fw, fw_src = (inv or {}).get("firmware"), (inv or {}).get("firmware_source")
        if not inv:
            fw, fw_src = view.get("firmware"), view.get("firmware_source")
        own_fw = (inv or {}).get("firmware_own")
        out = []
        if family:
            fv, fm, ff = family
            out.append({"vendor": fv or view.get("vendor"), "vendor_src": "family" if fv else view.get("vendor_source"),
                        "model": fm, "model_src": "family", "firmware": own_fw or ff or fw,
                        "fw_src": "own" if own_fw else ("family" if ff else fw_src)})
        if hub:
            hv, hm, hs = hub
            out.append({"vendor": hv or view.get("vendor"), "vendor_src": "hub" if hv else view.get("vendor_source"),
                        "model": hm, "model_src": "hub", "firmware": own_fw or hs or fw,
                        "fw_src": "own" if own_fw else ("hub" if hs else fw_src)})
        out.append({"vendor": view.get("vendor"), "vendor_src": view.get("vendor_source"), "model": view.get("model"),
                    "model_src": view.get("model_source"), "firmware": fw, "fw_src": fw_src})
        return out

    # ------------------------------------------------------------------ all devices
    def _compute(self, now: float) -> dict:
        feed = self.table()
        inv = getattr(self.core, "inventory", None)
        items = {}
        if inv is not None:
            for it in inv.list({}, now=now)["items"]:
                items[it["id"]] = it
        hub, fam = self._hub_facts(), self._family_models()
        dismissed = self._dismissed()
        devices = {}
        for view in self.core.devices():
            did = view["id"]
            if view.get("merged_into") or view["kind"] in ("internet", "self") or view.get("is_self"):
                continue
            it = items.get(did)
            if it and it["state"] == "decommissioned":
                continue
            res = match(self.candidates(view, it, hub.get(did), fam.get(did)), feed)
            found = []
            for f in res["findings"]:
                e = f["entry"]
                dk = dismissed.get("%s|%s" % (did, e["id"]))
                found.append({"id": e["id"], "severity": e["sev"], "score": e["score"], "exploited": bool(e["kev"]),
                              "exploited_since": e["kev"] or None, "ransomware": e["ransom"], "title": e["title"],
                              "action": e["action"], "certainty": f["certainty"], "reasons": f["reasons"], "fixed": f["fixed"],
                              "firmware": f["firmware"], "firmware_source": f["firmware_source"], "model": f["model"],
                              "vendor": f["vendor"],
                              "dismissed": ({"at": dk.get("at"), "by": dk.get("by") or "", "note": dk.get("note") or ""} if dk else None)})
            found.sort(key=sort_key)
            devices[did] = {"device": {"id": did, "title": view["title"] or "", "ip": view["ip"], "kind": view["kind"],
                                       "vendor": view.get("vendor"), "model": view.get("model"), "status": view.get("status")},
                            "status": res["status"], "reason": res["reason"], "findings": found}
        return {"devices": devices, "feed": self.feed_info(), "now": now}

    def _all(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        stamp = (getattr(self.core, "structure_version", 0), id(self.table()))
        with self._lock:
            c = self._cache
            if c and c[0] == stamp and now - c[1] < CACHE_S and now >= c[1]:
                return c[2]
        res = self._compute(now)
        with self._lock:
            self._cache = (stamp, now, res)
        return res

    def device(self, device_id: str, now: Optional[float] = None) -> Optional[dict]:
        res = self._all(now)
        d = res["devices"].get(device_id)
        if d is None:
            if not self.store.device(device_id):
                return None
            return {"device": {"id": device_id}, "status": "unchecked", "reason": "model_unknown", "findings": [], "feed": res["feed"]}
        return dict(d, feed=res["feed"])

    def list(self, query: Optional[dict] = None, now: Optional[float] = None) -> dict:
        q = query or {}
        res = self._all(now)
        rows, unchecked = [], []
        summary = {"open": 0, "exploited": 0, "certain": 0, "possible": 0, "dismissed": 0, "devices": 0, "checked": 0,
                   "unchecked": 0, "by_severity": {s: 0 for s in SEVERITIES}}
        affected = set()
        for did, d in res["devices"].items():
            if d["status"] == "unchecked":
                summary["unchecked"] += 1
                unchecked.append({"device": d["device"], "reason": d["reason"]})
            else:
                summary["checked"] += 1
            for f in d["findings"]:
                row = dict(f, device=d["device"])
                rows.append(row)
                if f["dismissed"]:
                    summary["dismissed"] += 1
                    continue
                summary["open"] += 1
                summary["by_severity"][f["severity"]] += 1
                summary["exploited"] += 1 if f["exploited"] else 0
                summary[f["certainty"]] += 1
                affected.add(did)
        summary["devices"] = len(affected)
        show = q.get("show")
        if show == "dismissed":
            rows = [r for r in rows if r["dismissed"]]
        else:
            rows = [r for r in rows if not r["dismissed"]]
            if show == "exploited":
                rows = [r for r in rows if r["exploited"]]
            elif show in CERTAINTIES:
                rows = [r for r in rows if r["certainty"] == show]
        sev = q.get("severity")
        if sev in SEVERITIES:
            rows = [r for r in rows if r["severity"] == sev]
        text = str(q.get("q") or "").strip().lower()[:80]
        if text:
            words = text.split()

            def hay(r):
                dv = r["device"]
                return " ".join(str(x or "") for x in (r["id"], r["title"], dv["title"], dv["ip"], r["vendor"], r["model"], r["firmware"])).lower()
            rows = [r for r in rows if all(w in hay(r) for w in words)]
        rows.sort(key=lambda r: sort_key(r) + ((r["device"]["title"] or r["device"]["ip"] or "").lower(),))
        unchecked.sort(key=lambda u: (UNCHECKED.index(u["reason"]) if u["reason"] in UNCHECKED else 9, (u["device"]["title"] or u["device"]["ip"] or "").lower()))
        return {"items": rows, "summary": summary, "unchecked": unchecked, "feed": res["feed"], "now": res["now"]}

    def open_findings(self, now: Optional[float] = None) -> list:
        """(device, finding) of everything not dismissed, most serious first."""
        res = self._all(now)
        out = [(d["device"], f) for d in res["devices"].values() for f in d["findings"] if not f["dismissed"]]
        out.sort(key=lambda x: sort_key(x[1]))
        return out

    # ------------------------------------------------------------------ news: first seen, announcements
    def _seen(self) -> dict:
        s = self.store.get_setting("vulns_seen", {}) or {}
        return s if isinstance(s, dict) else {}

    def watch(self, now: Optional[float] = None) -> list:
        """Remember when each finding was first seen and announce new ones (event "vulnerability"). Returns the new ones."""
        now = self.clock() if now is None else now
        seen = self._seen()
        current = {}
        new = []
        for dev, f in self.open_findings(now):
            key = "%s|%s" % (dev["id"], f["id"])
            current[key] = seen.get(key) or now
            if key not in seen:
                new.append((dev, f))
        known = set(self._all(now)["devices"])
        for key, t in seen.items():                  # a dismissed finding keeps its first sighting
            if key not in current and key.split("|")[0] in known:
                current[key] = t
        self.store.set_setting("vulns_seen", current)
        notify = getattr(self.core, "notify", None)
        if notify is not None and not getattr(self.core, "demo", None):
            for dev, f in new:
                try:
                    notify.emit("vulnerability", "vuln:%s:%s" % (dev["id"], f["id"]),
                                {"title": dev["title"] or dev["ip"] or "", "ip": dev["ip"] or "", "name": f["id"],
                                 "certainty_key": "vulns.certainty." + f["certainty"],
                                 "action_key": action_key(f).replace("update_to", "update")},   # a message text takes no nested values
                                severity="warning" if f["certainty"] == "certain" or f["exploited"] else "info",
                                device=dev["id"], t=now, once=True)
                except Exception:
                    log.exception("vulnerability hints: announcement failed")
        return new

    def first_seen(self) -> dict:
        return self._seen()

    async def loop(self) -> None:
        await asyncio.sleep(240)
        while True:
            try:
                await asyncio.to_thread(self.watch)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("vulnerability hints failed")
            await asyncio.sleep(3600)


def sort_key(f: dict) -> tuple:
    """Most serious first: severity, then exploited in the wild, then certain before possible, then the score."""
    return (SEV_RANK.get(f.get("severity"), 9), 0 if f.get("exploited") else 1, 0 if f.get("certainty") == "certain" else 1,
            -(f.get("score") or 0), f.get("id") or "")


def action_key(f: dict) -> str:
    """What to do, as an i18n key: update to the fixed firmware, look for an update, or disconnect/replace."""
    if f.get("action") == "replace" or (not f.get("fixed") and "all_versions" in (f.get("reasons") or [])):
        return "vulns.do.replace"
    return "vulns.do.update_to" if f.get("fixed") else "vulns.do.update"
