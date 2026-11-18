"""Secret filter of the host agent: only metadata leaves the host, never contents.

Two layers, both applied on the agent before anything is sent (and once more by Lanloom when a report arrives):

* **Positive lists.** Every record the agent builds is reduced to a fixed set of fields (``pick``). Anything a
  container runtime, the service manager or the kernel answers beyond those fields - environment variables, command
  lines, entry points, arguments, labels, mounts, health check output, unit descriptions - is never copied in the
  first place. Container labels pass only for the few compose names in ``LABELS``.
* **Value filter.** Every string that is kept still runs through ``text``: the password of ``scheme://user:pw@host``
  and long token-looking runs are replaced (``lanloom.scan.scrub``), ``key=value`` pairs whose key names a secret lose
  their value, and every string is cut to a short length.

The filter has no switch; a test feeds it a container whose environment, command line and labels carry known secrets
and checks that none of them reaches the report.
"""
from __future__ import annotations

import re

from ..scan.scrub import HIDDEN, scrub as _scrub

MAX_TEXT = 120
LABELS = ("com.docker.compose.project", "com.docker.compose.service", "io.podman.compose.project",  # identifier: runtime label names
          "io.podman.compose.service")  # identifier: runtime label name
_SECRET_KV = re.compile(r"(?i)\b([a-z0-9_.-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|auth|credential|private[_-]?key)"
                        r"[a-z0-9_.-]*)\s*([=:])\s*\S+")
# fields that must never appear in anything the agent sends, whatever their value (checked by the leak test as well)
FORBIDDEN_KEYS = frozenset({"env", "environment", "cmd", "command", "cmdline", "args", "argv", "entrypoint", "labels",
                            "mounts", "binds", "volumes", "config", "hostconfig", "log", "output", "description"})


def text(value, limit: int = MAX_TEXT):
    """A kept string: secrets removed, cut to ``limit`` characters. None stays None, other types become strings."""
    if value is None:
        return None
    s = str(value).replace("\x00", "")
    s = _SECRET_KV.sub(lambda m: m.group(1) + m.group(2) + HIDDEN, s)
    s = _scrub(s)
    s = "".join(ch if ch.isprintable() else " " for ch in s)
    return s[:limit]


def num(value, lo=None, hi=None):
    """A kept number (int or float) or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(value) if "." in str(value) else int(value)
        except (TypeError, ValueError):
            return None
    if value != value:                      # NaN
        return None
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        value = hi
    return value


def pick(src: dict, spec: dict) -> dict:
    """Copy only the fields of ``spec`` ({name: "text"|"num"|"bool"|"ip"}) from ``src``; everything else is dropped."""
    out = {}
    for key, kind in spec.items():
        if key not in src:
            continue
        v = src[key]
        if kind == "num":
            v = num(v)
        elif kind == "bool":
            v = bool(v)
        elif kind == "ip":
            v = text(v, 45)
        else:
            v = text(v)
        if v is not None:
            out[key] = v
    return out


def labels(raw) -> dict:
    """Only the compose project and service names of a container's labels."""
    out = {}
    if isinstance(raw, dict):
        for k in LABELS:
            if raw.get(k):
                out["service" if k.endswith("service") else "project"] = text(raw[k], 64)
    return out


def check(obj, path: str = "") -> list:
    """Paths of forbidden keys anywhere in ``obj`` (empty = clean). Used before sending and by the server."""
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                bad.append(path + "/" + str(k))
            bad.extend(check(v, path + "/" + str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5000]):
            bad.extend(check(v, "%s[%d]" % (path, i)))
    return bad


def strip(obj):
    """``obj`` without forbidden keys and with every string filtered (the server's second pass)."""
    if isinstance(obj, dict):
        return {str(k)[:64]: strip(v) for k, v in obj.items() if str(k).lower() not in FORBIDDEN_KEYS}
    if isinstance(obj, list):
        return [strip(v) for v in obj[:5000]]
    if isinstance(obj, str):
        return text(obj, 200)
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return None
