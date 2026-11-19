""""Waiting on someone": a lasting condition that only a person can end should not drown out new problems.

A row of the overview's noteworthy list moves into its own state ``waiting`` when both hold:

1. It needs a person's hand. The row says so itself (``"human": True``): an infrastructure device that is off (someone
   has to switch it on or plug it in), a connection whose sign-in or certificate is refused (someone has to change the
   settings), new devices nobody has named yet. Features that add rows with ``register_note`` set the flag where it fits.
2. It has been unchanged for more than ``THRESHOLD_S`` (24 hours): same id, same level, same text key and the same
   values. Values that change by the clock alone (``*_dur``, ``*_at``, ``age``) do not count.

Safe in doubt: any difference in that key (a second device, warning to danger, another reason) ends ``waiting`` in the
same round and starts the 24 hours again; a key that changes too often only costs waiting time, a too coarse one would
hide a problem. ``waiting`` rows do not set the overall level (they drown out nothing) and are listed on their own line.

The memory lives in the settings (``waiting_notes``), so a restart does not start the 24 hours again; it is written at
most once a minute (new rows at once). Without a round for more than ``GAP_S`` (6 hours, e.g. Lanloom was stopped),
"unchanged" is no longer proven and the clock starts again.
"""
from __future__ import annotations

import json
from typing import Optional

SETTING = "waiting_notes"
THRESHOLD_S = 24 * 3600
GAP_S = 6 * 3600
SAVE_EVERY_S = 60
MAX_ENTRIES = 500
CLOCK_VARS = ("_dur", "_at")
CLOCK_NAMES = ("age", "t", "now", "when")


def note_key(note: dict) -> str:
    """The part of a row that must stay the same for 24 hours (values that move with the clock left out)."""
    vars_ = {k: v for k, v in sorted((note.get("vars") or {}).items())
             if not k.endswith(CLOCK_VARS) and k not in CLOCK_NAMES}
    return json.dumps([note.get("id"), note.get("level"), note.get("key"), vars_], sort_keys=True, default=str)[:800]


def apply(notes: list, memory: dict, now: float, backdate=None) -> tuple:
    """-> (active rows, waiting rows, new memory). ``memory``: {row id: {"k", "since", "last"}}. ``backdate``: optional
    function row -> start time for a row seen for the first time (the sample network's long-standing rows)."""
    active, waiting, new = [], [], {}
    for n in notes:
        if not n.get("human"):
            active.append(n)
            continue
        nid = str(n.get("id"))
        k = note_key(n)
        old = memory.get(nid)
        if old and old.get("k") == k and now - float(old.get("last") or 0) <= GAP_S:
            entry = {"k": k, "since": float(old["since"]), "last": now}
        else:
            first = backdate(n) if (backdate and not old) else None
            entry = {"k": k, "since": float(first) if first else now, "last": now}
        new[nid] = entry
        if now - entry["since"] > THRESHOLD_S:
            waiting.append(dict(n, waiting={"since": round(entry["since"]), "level": n.get("level")}, level="waiting"))
        else:
            active.append(n)
    if len(new) > MAX_ENTRIES:
        new = dict(sorted(new.items(), key=lambda kv: -kv[1]["since"])[:MAX_ENTRIES])
    waiting.sort(key=lambda n: (n["waiting"]["since"], n["id"]))
    return active, waiting, new


class Waiting:
    """Keeps the memory between rounds and in the settings."""

    def __init__(self, store, clock=None):
        self.store = store
        self.memory = {}
        self._saved = 0.0
        self._loaded = False
        self.backdate = None             # the demo sets this: rows of the sample that have been there for days

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        raw = self.store.get_setting(SETTING, {}) if self.store is not None else {}
        if isinstance(raw, dict):
            self.memory = {str(k): v for k, v in raw.items() if isinstance(v, dict) and "k" in v and "since" in v}

    def split(self, notes: list, now: float) -> tuple:
        """-> (active rows, waiting rows)."""
        self._load()
        active, waiting, new = apply(notes, self.memory, now, self.backdate)
        fresh_ids = set(new) - set(self.memory)
        changed = fresh_ids or set(self.memory) - set(new) or any(self.memory[i]["k"] != new[i]["k"] for i in new if i in self.memory)
        self.memory = new
        if self.store is not None and (changed or now - self._saved >= SAVE_EVERY_S):
            self._saved = now
            try:
                self.store.set_setting(SETTING, new)
            except Exception:
                pass
        return active, waiting

    def is_waiting(self, note: dict, now: float) -> bool:
        """Read only (for the live rail): does this row wait on a person right now?"""
        self._load()
        e = self.memory.get(str(note.get("id")))
        return bool(note.get("human") and e and e["k"] == note_key(note) and now - e["since"] > THRESHOLD_S)

    def since(self, note_id: str) -> Optional[float]:
        e = self.memory.get(str(note_id))
        return e["since"] if e else None
