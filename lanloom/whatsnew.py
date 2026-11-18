""""What's new?": the changes of each version, read from the CHANGELOG.md that ships in the image.

Nothing is fetched: the panel shows what this image itself says about its versions. Each signed-in user has a
"seen" mark (the newest version they looked at, kept in their preferences); versions above it and up to the running
one are new for them, and the version number in the header carries a dot until they open the panel.

A user who never opened the panel starts at the running version (no dot on a first sign-in), so the dot means
"this installation changed since you last looked".
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Optional

from . import __version__

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
HEAD = re.compile(r"^##\s+\[(\d{1,6}\.\d{1,6}\.\d{1,6})\](?:\s*[–—-]\s*(\S+))?")
SECTION = re.compile(r"^###\s+(.+?)\s*$")
ITEM = re.compile(r"^(\s*)[-*]\s+(.*)$")
LINK = re.compile(r"\[([^\]]+)\]\((?:https?://|#)[^)]*\)")
DEFAULT_LIMIT = 12
MAX_LIMIT = 200
ITEM_MAX = 2000

_lock = threading.Lock()
_cache: dict = {"key": None, "entries": []}


def _version(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0, 0, 0)


def _clean(text: str) -> str:
    text = LINK.sub(r"\1", text.strip())          # no outbound links in the panel, the words stay
    return text[:ITEM_MAX]


def parse(text: str) -> list:
    """Changelog text -> [{version, date, sections: [{title, items: [...]}]}], in file order (newest first)."""
    entries: list = []
    cur = None
    sec = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = HEAD.match(line)
        if m:
            cur = {"version": m.group(1), "date": m.group(2) or "", "sections": []}
            entries.append(cur)
            sec = None
            continue
        if cur is None:
            continue
        if line.startswith("## "):         # a heading that is not a version (e.g. "Unreleased") ends the entry
            cur, sec = None, None
            continue
        m = SECTION.match(line)
        if m:
            sec = {"title": _clean(m.group(1)), "items": []}
            cur["sections"].append(sec)
            continue
        if not line.strip():
            continue
        m = ITEM.match(line)
        if sec is None:
            sec = {"title": "", "items": []}
            cur["sections"].append(sec)
        if m:
            sec["items"].append(_clean(m.group(2)))
        elif sec["items"] and raw[:1].isspace():
            sec["items"][-1] = _clean(sec["items"][-1] + " " + line.strip())
        else:
            sec["items"].append(_clean(line))
    for e in entries:
        e["sections"] = [s for s in e["sections"] if s["items"]]
    return entries


def entries(path: Path = CHANGELOG) -> list:
    """Parsed changelog, cached until the file changes. Missing file -> []."""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    with _lock:
        if _cache["key"] != key:
            try:
                _cache["entries"] = parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                _cache["entries"] = []
            _cache["key"] = key
        return _cache["entries"]


def view(seen: Optional[str], limit: int = DEFAULT_LIMIT, path: Path = CHANGELOG) -> dict:
    cur = _version(__version__)
    seen_v = _version(seen) if seen else cur
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    # versions newer than the running one (a changelog ahead of the image) are left out
    listed = [e for e in entries(path) if _version(e["version"]) <= cur]
    out = []
    for e in listed[:limit]:
        v = _version(e["version"])
        out.append(dict(e, current=e["version"] == __version__, new=seen_v < v <= cur))
    return {"current": __version__, "seen": seen, "unseen": seen_v < cur and any(_version(e["version"]) > seen_v for e in listed),
            "more": len(listed) > limit, "entries": out}


def newer(a: Optional[str], b: Optional[str]) -> bool:
    """True when version a is newer than b (missing b counts as older)."""
    return bool(a) and (not b or _version(a) > _version(b))
