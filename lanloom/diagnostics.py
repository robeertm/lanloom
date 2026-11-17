"""Diagnostic export: one redacted JSON file an administrator can attach to an issue (Settings > General).

What goes in: the version and the kind of installation, switches (on/off, never their values), counts (devices per kind,
accounts per role, reports), the state of every source with its *reason key* and the age of its last sign of life, the last
scan in numbers, the event counters of the last day, and the state of backup and update check.

What never goes in: MAC and IP addresses, host, device, user, network and source names, addresses of controllers or hubs,
credentials, tokens, keys, notes, paths, the contents of the nightly reports. Sources are numbered ("snmp-1"), not named.

Two layers: the sections above only pick numbers, flags and fixed vocabulary; then ``redact`` walks the whole result and
replaces anything that still looks like a hardware address, an IP address, a mail address or a web address, and drops every
string that is not plain vocabulary. The count of such replacements is part of the file, so a regression is visible.
"""
from __future__ import annotations

import os
import platform
import re
import sys
import time
from typing import Any

from . import __version__

FORMAT = "lanloom-diagnostics/1"

_MAC = re.compile(r"(?i)\b[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}\b|\b[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}\b")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_IPV6 = re.compile(r"(?i)(?<![0-9a-z])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![0-9a-z])")
_MAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+")
# plain vocabulary, one word: reason keys ("source.hub.unreachable"), kinds, states, versions, dates ("2026-11-17T09:30Z")
_PLAIN = re.compile(r"^[A-Za-z0-9_.:+/\-]{0,80}$")


def _scrub(text: str) -> tuple:
    n = 0
    for rx, repl in ((_URL, "<url>"), (_MAIL, "<mail>"), (_MAC, "<mac>"), (_IPV6, "<ip>"), (_IPV4, "<ip>")):
        text, k = rx.subn(repl, text)
        n += k
    return text, n


def redact(value: Any) -> tuple:
    """(clean copy, number of replacements). Strings that are not plain vocabulary are dropped altogether."""
    if isinstance(value, dict):
        out, total = {}, 0
        for k, v in value.items():
            clean, n = redact(v)
            ks, kn = _scrub(str(k))
            out[ks if _PLAIN.match(ks) else "<key>"] = clean
            total += n + kn
        return out, total
    if isinstance(value, (list, tuple)):
        out, total = [], 0
        for v in value:
            clean, n = redact(v)
            out.append(clean)
            total += n
        return out, total
    if isinstance(value, str):
        text, n = _scrub(value)
        if not _PLAIN.match(text):
            return "<removed>", n + 1
        return text, n
    if isinstance(value, (bool, int, float)) or value is None:
        return value, 0
    return "<removed>", 1


def _safe(fn):
    try:
        return fn()
    except Exception:       # a broken section must not stop the export; the file says which one
        return {"unavailable": True}


def _age_bucket(age) -> str:
    """Coarse on purpose: the exact time of a sign of life is not needed to understand a failure."""
    if age is None:
        return "never"
    for limit, name in ((60, "under_1_min"), (600, "under_10_min"), (3600, "under_1_h"), (86400, "under_1_day")):
        if age < limit:
            return name
    return "over_1_day"


def _devices(core) -> dict:
    rows = core.store.query("SELECT kind, user_kind, hidden, mac IS NOT NULL AS has_mac FROM devices")
    kinds: dict = {}
    for r in rows:
        k = r["user_kind"] or r["kind"] or "unknown"
        kinds[k] = kinds.get(k, 0) + 1
    last = core._last_scan_id()
    status: dict = {}
    for dev in core.store.devices():
        s = core.device_status(dev, last)
        status[s] = status.get(s, 0) + 1
    return {"total": len(rows), "hidden": sum(1 for r in rows if r["hidden"]), "without_hardware_address": sum(1 for r in rows if not r["has_mac"]),
            "per_kind": dict(sorted(kinds.items())), "per_status": dict(sorted(status.items()))}


def _sources(core, now: float) -> list:
    from .overview import source_state
    out = []
    if "host" in core.cadence or not core.demo:
        s = source_state(core, "host", now)
        out.append({"source": "host", "family": "host", "state": s["state"], "reason": s.get("key") or "", "grade": s["grade"],
                    "last_sign_of_life": _age_bucket(s["age_s"]), "cadence_s": s["cadence_s"]})
    counts: dict = {}
    for integ in core.integrations():
        key = "int:%d" % integ["id"]
        s = source_state(core, key, now, integ["enabled"])
        counts[integ["kind"]] = counts.get(integ["kind"], 0) + 1
        out.append({"source": "%s-%d" % (integ["kind"], counts[integ["kind"]]), "family": integ["kind"], "enabled": bool(integ["enabled"]),
                    "state": s["state"], "reason": s.get("key") or (integ["status"] or {}).get("key") or "", "grade": s["grade"],
                    "last_sign_of_life": _age_bucket(s["age_s"]), "cadence_s": s["cadence_s"],
                    "secrets_set": sum(1 for v in integ["secrets"].values() if v), "settings_filled": len(integ["config"])})
    return out


def _scans(core, now: float) -> dict:
    rows = core.store.query("SELECT started, finished, mode, status, hosts FROM scans ORDER BY id DESC LIMIT 5")
    return {"count": (core.store.one("SELECT COUNT(*) AS n FROM scans") or {}).get("n", 0),
            "latest": [{"mode": r["mode"], "status": r["status"], "hosts": r["hosts"], "age_h": round((now - r["started"]) / 3600, 1),
                        "duration_s": round(r["finished"] - r["started"], 1) if r["finished"] else None} for r in rows]}


def _events(core, now: float) -> dict:
    rows = core.store.query("SELECT kind, COUNT(*) AS n FROM events WHERE t > ? GROUP BY kind ORDER BY kind", (now - 86400,))
    return {r["kind"]: r["n"] for r in rows}


def _accounts(state) -> dict:
    rows = state.store.query("SELECT role, COUNT(*) AS n FROM users GROUP BY role ORDER BY role")
    return {"per_role": {r["role"]: r["n"] for r in rows}}


def _reports(core, now: float) -> dict:
    row = core.store.one("SELECT COUNT(*) AS n, MAX(created) AS last FROM reports") or {}
    return {"count": row.get("n", 0), "latest_age_h": round((now - row["last"]) / 3600, 1) if row.get("last") else None}


def _settings(core) -> dict:
    s = core.settings()
    flags = {k: bool(v) for k, v in s.items() if isinstance(v, bool)}
    lists = {k: len(v) for k, v in s.items() if isinstance(v, list)}
    plain = {k: s[k] for k in ("rescan_hours", "rate_unit", "language") if k in s}
    return {"switches": flags, "list_lengths": lists, **plain}


def _runtime(cfg) -> dict:
    return {"python": "%d.%d.%d" % sys.version_info[:3], "system": platform.system(), "machine": platform.machine(),
            "in_container": os.path.exists("/.dockerenv") or os.environ.get("container") is not None,
            "cpu_count": os.cpu_count(), "data_dir_writable": os.access(cfg.data_dir, os.W_OK)}


def _config(cfg) -> dict:
    return {"demo": cfg.demo, "probe": cfg.probe, "update_check": cfg.update_check, "cookie_secure": cfg.cookie_secure,
            "trusted_origins": len(cfg.trusted_origins), "trusted_proxies": len(cfg.trusted_proxies),
            "auth_proxy": bool(cfg.auth_header), "base_path": bool(cfg.base_path), "port_is_default": cfg.port == 7380}


def _backup_update(state) -> dict:
    b = state.backups.public_state()
    u = state.updates.state()
    return {"backup": {"schedule_on": bool((b.get("schedule") or {}).get("enabled")), "files": len(b.get("files") or []),
                       "last_ok": bool(b.get("last_ok")), "last_error": str(b.get("last_error") or "")[:40], "stale": bool(b.get("stale"))},
            "update": {"current": u.get("current"), "latest": u.get("latest"), "available": bool(u.get("available")),
                       "check_enabled": bool(u.get("enabled")), "sidecar": bool(u.get("sidecar")), "check_error": bool(u.get("error"))}}


def build(state, now: float | None = None) -> dict:
    """The redacted diagnostic document for the running app (``state`` is main.App)."""
    now = time.time() if now is None else now
    core, cfg = state.core, state.cfg
    doc = {
        "format": FORMAT,
        "version": __version__,
        "generated": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(now)),
        "runtime": _safe(lambda: _runtime(cfg)),
        "config": _safe(lambda: _config(cfg)),
        "settings": _safe(lambda: _settings(core)),
        "devices": _safe(lambda: _devices(core)),
        "sources": _safe(lambda: _sources(core, now)),
        "scans": _safe(lambda: _scans(core, now)),
        "events_24h": _safe(lambda: _events(core, now)),
        "accounts": _safe(lambda: _accounts(state)),
        "reports": _safe(lambda: _reports(core, now)),
        "backup_and_update": _safe(lambda: _backup_update(state)),
    }
    clean, n = redact(doc)
    clean["redaction"] = {"replaced": n, "note": "no addresses, names, credentials or notes are collected; check the file before you share it"}
    return clean
