"""End-of-updates hints from an offline table. Never asks anything on the network.

The image ships ``lanloom/data/eol.json``: product or release -> the date the vendor's regular updates end. It is
renewed with every Lanloom release (the update feed), never downloaded at run time. A table of the operator's own can be
dropped into the data directory (``<data>/eol/eol.json``, same format); its entries are added, and an entry with the
same id replaces the bundled one. A device's own end date (inventory field) always wins over the table.

A match is advisory: patterns are tested against the lower-cased maker, model, firmware and system description the
device reported. The table is validated when it is loaded (dates, patterns, ids); a bad entry is skipped and logged, a
bad file never breaks the start.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("lanloom")

DATA_FILE = Path(__file__).resolve().parent / "data" / "eol.json"
MAX_ENTRIES = 2000
MAX_PATTERN = 160


def parse_date(value) -> Optional[datetime.date]:
    """A calendar date ``YYYY-MM-DD`` between 1990 and 2100, else None."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        d = datetime.date.fromisoformat(value)
    except ValueError:
        return None
    return d if 1990 <= d.year <= 2100 else None


def clean_entry(raw) -> Optional[dict]:
    """One validated entry ``{id, name, until, rx: [compiled patterns]}`` or None."""
    if not isinstance(raw, dict):
        return None
    eid, name, until = raw.get("id"), raw.get("name", ""), raw.get("until")
    pats = raw.get("match")
    if not (isinstance(eid, str) and re.fullmatch(r"[a-z0-9_.-]{1,40}", eid)) or not isinstance(name, str):
        return None             # the name is optional: the bundled table carries ids only, an own table may name its entries
    if parse_date(until) is None or not isinstance(pats, list) or not pats:
        return None
    rx = []
    for p in pats[:8]:
        if not isinstance(p, str) or not p or len(p) > MAX_PATTERN:
            return None
        try:
            rx.append(re.compile(p))
        except re.error:
            return None
    sample = raw.get("sample")
    return {"id": eid, "name": name.strip()[:60], "until": until, "rx": rx, "sample": sample if isinstance(sample, str) else ""}


def load_table(path: Path) -> tuple:
    """(as_of, [entries]) from one file; ``(None, [])`` for a missing or broken file."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, []
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        return None, []
    out, seen = [], set()
    for raw in doc["entries"][:MAX_ENTRIES]:
        e = clean_entry(raw)
        if e is None:
            log.warning("end-of-updates table: skipped an invalid entry (%s)", str((raw or {}).get("id") if isinstance(raw, dict) else raw)[:40])
            continue
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        out.append(e)
    as_of = doc.get("as_of") if parse_date(doc.get("as_of")) else None
    return as_of, out


class EolTable:
    def __init__(self, entries: Optional[list] = None, as_of: Optional[str] = None, extra: Optional[Path] = None):
        if entries is None:
            as_of, entries = load_table(DATA_FILE)
        self.as_of = as_of
        merged = {e["id"]: e for e in entries}
        if extra is not None and Path(extra).is_file():
            extra_as_of, more = load_table(Path(extra))
            for e in more:
                merged[e["id"]] = e
            if extra_as_of and (not as_of or extra_as_of > as_of):
                self.as_of = extra_as_of
        self.entries = list(merged.values())

    @classmethod
    def from_raw(cls, raw_entries: list, as_of: Optional[str] = None) -> "EolTable":
        return cls([e for e in (clean_entry(r) for r in raw_entries) if e], as_of)

    def lookup(self, *texts) -> Optional[dict]:
        """The entry whose pattern matches the given texts; of several matches the one with the latest date (the most
        specific release wins by pattern, so this only decides ties)."""
        hay = " ".join(str(t).lower() for t in texts if t)[:1500]
        if not hay.strip():
            return None
        hits = [e for e in self.entries if any(r.search(hay) for r in e["rx"])]
        if not hits:
            return None
        e = max(hits, key=lambda x: x["until"])
        return {"id": e["id"], "name": e["name"] or None, "until": e["until"]}
