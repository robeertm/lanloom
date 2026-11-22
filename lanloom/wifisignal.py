"""Signal strength of wireless clients (W2): one cleaning rule for every adapter and the three grades of the access
point view. Nothing here is stored - the readings live in memory only (see ``portfront.RoamWatch``)."""
import re

MIN_DBM, MAX_DBM = -100, -10        # outside this range a reading is a unit mix-up or a dead field and is dropped
GOOD_FROM = -65                     # good: -65 dBm and stronger
FAIR_FROM = -75                     # fair: down to -75 dBm, weak below
STICK_DBM = -80                     # a device at this level or weaker may be stuck to an access point
STICK_S = 600                       # ... when it stays there this long (10 minutes)

_NUM = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


def clean(v):
    """A signal reading as whole dBm, or None. Accepts numbers and texts such as ``"-62"`` or ``"-62@5GHz"``; a
    positive value, a percentage or anything outside -100..-10 is not a dBm reading and is dropped (never guessed)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        m = _NUM.match(v)
        v = float(m.group(1)) if m else None
    if not isinstance(v, (int, float)) or v != v:
        return None
    n = int(round(v))
    return n if MIN_DBM <= n <= MAX_DBM else None


def grade(dbm) -> str:
    """``good`` / ``fair`` / ``weak`` for a cleaned reading."""
    return "good" if dbm >= GOOD_FROM else "fair" if dbm >= FAIR_FROM else "weak"
