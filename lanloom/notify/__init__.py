"""Notification rules: who gets what, when - calm instead of loud.

Lanloom heals itself where it can and tells you about the rest in the nightly report. Everything else is a choice of
each person, per event (see ``events.py``):

* **mode** - ``nightly`` (default: nothing is sent, the nightly report covers it), ``hourly`` (one bundled message at
  the top of the hour), ``immediate`` or ``off``; a **minimum severity** below which the event stays in the report;
  and the **channels** (web push on this person's devices, e-mail, webhook, push topic, chat bot - the shared channels
  are the ones an administrator set up for the nightly report).
* **quiet hours** per person (for example 22:00-07:00): only *immediate* + *critical* passes, the rest stays in the
  nightly report.
* **maintenance** (one-off or weekly, for everything or some devices): nothing is announced, the occurrences
  are marked as maintenance (``maintenance_at`` for incident handling), and no all-clear follows.
* **flap protection**: conditions with ``hold`` must last ``hold_rounds`` watch rounds (one per minute) before they are
  announced; a condition that clears earlier was only a flap. After a message the same subject stays quiet for
  ``cooldown_h`` hours (the next occurrence goes into the nightly report). An all-clear is only sent after an announced
  outage. A **daily limit** per person caps the number of messages; past it, one note says so.
* **heartbeat** (optional): Lanloom calls an address of your choice regularly, so an outside service notices when
  Lanloom itself is down.

Nothing leaves the host before somebody sets up a channel. Secrets (VAPID key, push endpoints, the heartbeat address)
live in the encrypted secret store and never appear in a log or an answer.
"""
import asyncio
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
import ssl
import socket
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .. import __version__
from . import events as ev_mod
from .events import DEFAULT_MODE, MODES, RANK, SEVERITIES, Event, register  # noqa: F401 - public register

log = logging.getLogger("lanloom")

SHARED = ("email", "webhook", "ntfy", "chatbot")
CHANNELS = ("webpush",) + SHARED
DEFAULTS = {"daily_cap": 20, "hold_rounds": 3, "cooldown_h": 12, "protected_cidrs": []}
CAP_CHOICES = (5, 10, 20, 50, 100)
HOLD_CHOICES = (1, 2, 3, 5, 10, 15)
COOLDOWN_CHOICES = (1, 3, 6, 12, 24)
ROUND_S = 60
HB_CHOICES = (1, 2, 5, 10, 15, 30, 60)
HB_TIMEOUT = 10
MAX_WINDOWS = 50
MAX_PUSH_PER_USER = 10
KEEP_LOG_DAYS = 30
KEEP_LOG_ROWS = 3000
TEST_GAP_S = 10
TIME_RE = re.compile(r"\A([01]\d|2[0-3]):([0-5]\d)\Z")
URL_RE = re.compile(r"\Ahttps?://[^\s]{3,500}\Z")
EMAIL_RE = re.compile(r"\A[^@\s,;<>]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}\Z")
STATUSES = ("pending", "flap", "sent", "bundled", "failed", "nightly", "quiet", "maintenance", "cooldown", "capped",
            "nochannel")



def _hm(text: str) -> int:
    hh, mm = text.split(":")
    return int(hh) * 60 + int(mm)


def in_span(minute: int, start: str, end: str) -> bool:
    """Is the minute of the day inside [start, end) (crossing midnight when end <= start)?"""
    a, b = _hm(start), _hm(end)
    if a == b:
        return False
    return a <= minute < b if a < b else (minute >= a or minute < b)


class Notifier:
    def __init__(self, core, clock: Callable[[], float] = time.time):
        self.core = core
        self.store = core.store
        self.vault = core.vault
        self.clock = clock
        self._lock = threading.RLock()
        self._outbox: deque = deque()
        self.sender: Optional[Callable] = None          # (channel, user, message) -> (ok, key); replaced in tests
        self.heartbeat_open: Optional[Callable] = None  # (url) -> None, raises on failure; replaced in tests
        self.taps: list = []                            # (eid, key, data, resolve, t) - e.g. the webhooks; called once per new event
        self._last_test: dict = {}
        self._last_periodic = 0.0
        self._hb_next = 0.0
        self._fail_until = 0.0

    # ------------------------------------------------------------------ people and channels of the instance
    # A further site (lanloom/sites.py) keeps its events, state, maintenance window list and log in its own store; the people,
    # their personal rules and push devices, and the shared channels belong to the instance and live at home.
    @property
    def people(self):
        return getattr(self.core, "people_store", None) or self.store

    @property
    def pvault(self):
        return getattr(self.core, "people_vault", None) or self.vault

    @property
    def channels(self):
        """The report settings whose shared channels (e-mail, webhook ...) deliver: the home site's for every site."""
        return getattr(self.core, "channel_reports", None) or getattr(self.core, "reports", None)

    def site_title(self) -> str:
        """The name of a further site in front of a message title ("" at home or with a single site)."""
        hub = getattr(self.core, "site_hub", None)
        if hub is None or not getattr(self.core, "remote", False):
            return ""
        return hub.name(getattr(self.core, "site_id", "")) or ""

    # ------------------------------------------------------------------ settings (shared)
    def settings(self) -> dict:
        raw = self.store.get_setting("notify_cfg", {}) or {}
        out = dict(DEFAULTS)
        out.update({k: raw[k] for k in DEFAULTS if k in raw})
        return out

    def update_settings(self, patch: dict) -> dict:
        cur = self.settings()
        for key, choices in (("daily_cap", CAP_CHOICES), ("hold_rounds", HOLD_CHOICES), ("cooldown_h", COOLDOWN_CHOICES)):
            if key in patch:
                if patch[key] not in choices or isinstance(patch[key], bool):
                    raise ValueError("notify.err.choice")
                cur[key] = patch[key]
        if "protected_cidrs" in patch:
            import ipaddress
            nets = []
            for c in patch["protected_cidrs"] or []:
                try:
                    nets.append(str(ipaddress.ip_network(str(c).strip(), strict=False)))
                except ValueError:
                    raise ValueError("notify.err.cidr")
            if len(nets) > 32:
                raise ValueError("notify.err.cidr")
            cur["protected_cidrs"] = sorted(set(nets))
        self.store.set_setting("notify_cfg", cur)
        return cur

    def protected(self, ip: str) -> bool:
        import ipaddress
        try:
            addr = ipaddress.ip_address(ip or "")
        except ValueError:
            return False
        for c in self.settings()["protected_cidrs"]:
            try:
                if addr in ipaddress.ip_network(c):
                    return True
            except ValueError:
                continue
        return False

    # ------------------------------------------------------------------ per-person rules
    def prefs(self, user_id: int) -> dict:
        raw = self.people.get_setting("notify_user:%d" % int(user_id), {}) or {}
        rules = {}
        for e in ev_mod.ordered():
            r = (raw.get("rules") or {}).get(e.id) or {}
            rules[e.id] = {"mode": r.get("mode") if r.get("mode") in MODES else DEFAULT_MODE,
                           "min": r.get("min") if r.get("min") in SEVERITIES else "info",
                           "channels": [c for c in (r.get("channels") or []) if c in CHANNELS]}
        quiet = raw.get("quiet") or {}
        return {"rules": rules,
                "quiet": {"enabled": bool(quiet.get("enabled")), "from": quiet.get("from") or "22:00", "to": quiet.get("to") or "07:00"},
                "email": raw.get("email") or ""}

    def update_prefs(self, user_id: int, patch: dict) -> dict:
        cur = self.prefs(user_id)
        for eid, r in (patch.get("rules") or {}).items():
            if eid not in ev_mod.REGISTRY or not isinstance(r, dict):
                raise ValueError("notify.err.event")
            rule = dict(cur["rules"][eid])
            if "mode" in r:
                if r["mode"] not in MODES:
                    raise ValueError("notify.err.mode")
                rule["mode"] = r["mode"]
            if "min" in r:
                if r["min"] not in SEVERITIES:
                    raise ValueError("notify.err.severity")
                rule["min"] = r["min"]
            if "channels" in r:
                chans = r["channels"] if isinstance(r["channels"], list) else []
                if any(c not in CHANNELS for c in chans):
                    raise ValueError("notify.err.channel")
                rule["channels"] = [c for c in CHANNELS if c in chans]
            cur["rules"][eid] = rule
        if "quiet" in patch and isinstance(patch["quiet"], dict):
            q = dict(cur["quiet"])
            for k in ("from", "to"):
                if k in patch["quiet"]:
                    if not isinstance(patch["quiet"][k], str) or not TIME_RE.match(patch["quiet"][k]):
                        raise ValueError("notify.err.time")
                    q[k] = patch["quiet"][k]
            if "enabled" in patch["quiet"]:
                q["enabled"] = bool(patch["quiet"]["enabled"])
            if q["enabled"] and q["from"] == q["to"]:
                raise ValueError("notify.err.quiet_same")
            cur["quiet"] = q
        if "email" in patch:
            mail = str(patch["email"] or "").strip()
            if mail and not EMAIL_RE.match(mail):
                raise ValueError("report.err.email_address")
            cur["email"] = mail[:254]
        stored = {"rules": {k: v for k, v in cur["rules"].items() if v != {"mode": DEFAULT_MODE, "min": "info", "channels": []}},
                  "quiet": cur["quiet"], "email": cur["email"]}
        self.people.set_setting("notify_user:%d" % int(user_id), stored)
        return self.prefs(user_id)

    def users(self) -> list:
        try:
            rows = self.people.query("SELECT id, username, role, prefs FROM users WHERE disabled=0 ORDER BY id")
        except Exception:
            return []
        for r in rows:
            try:
                r["lang"] = (json.loads(r.pop("prefs") or "{}") or {}).get("language")
            except ValueError:
                r["lang"] = None
        hub = getattr(self.core, "site_hub", None)
        if hub is not None and hub.several():
            rows = hub.recipients(getattr(self.core, "site_id", "home"), rows)   # only people who may see this site
        return rows

    # ------------------------------------------------------------------ time helpers
    def tz(self):
        try:
            return self.core.reports.tz()
        except Exception:
            return timezone.utc

    def local(self, t: float) -> datetime:
        return datetime.fromtimestamp(t, self.tz())

    def quiet_now(self, prefs: dict, t: float) -> bool:
        q = prefs["quiet"]
        if not q["enabled"]:
            return False
        lt = self.local(t)
        return in_span(lt.hour * 60 + lt.minute, q["from"], q["to"])

    # ------------------------------------------------------------------ planned maintenance
    def maint_list(self) -> list:
        return list(self.store.get_setting("notify_maint", []) or [])

    def add_window(self, data: dict, by: str = "") -> dict:
        kind = data.get("kind")
        title = str(data.get("title") or "").strip()[:80]
        devices = data.get("devices") or []
        if not isinstance(devices, list) or len(devices) > 200 or any(not isinstance(d, str) or len(d) > 80 for d in devices):
            raise ValueError("notify.err.devices")
        w = {"id": "w%x" % int(self.clock() * 1000), "kind": kind, "title": title, "devices": devices, "by": str(by)[:64],
             "created": self.clock()}
        if kind == "once":
            try:
                start, end = float(data["start"]), float(data["end"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("notify.err.window_time")
            if end <= start or end - start > 14 * 86400 or end <= self.clock():
                raise ValueError("notify.err.window_time")
            w.update(start=start, end=end)
        elif kind == "weekly":
            days = data.get("days")
            if not isinstance(days, list) or not days or any(d not in range(7) or isinstance(d, bool) for d in days):
                raise ValueError("notify.err.window_days")
            if not all(isinstance(data.get(k), str) and TIME_RE.match(data[k]) for k in ("from", "to")) or data["from"] == data["to"]:
                raise ValueError("notify.err.window_time")
            w.update(days=sorted(set(days)), **{"from": data["from"], "to": data["to"]})
        else:
            raise ValueError("notify.err.window_kind")
        ws = [x for x in self.maint_list() if not (x.get("kind") == "once" and x.get("end", 0) < self.clock() - 30 * 86400)]
        if len(ws) >= MAX_WINDOWS:
            raise ValueError("notify.err.window_many")
        ws.append(w)
        self.store.set_setting("notify_maint", ws)
        return w

    def remove_window(self, wid: str) -> bool:
        ws = self.maint_list()
        keep = [w for w in ws if w.get("id") != wid]
        if len(keep) == len(ws):
            return False
        self.store.set_setting("notify_maint", keep)
        return True

    def window_active(self, w: dict, t: float) -> bool:
        if w.get("kind") == "once":
            return float(w.get("start", 0)) <= t < float(w.get("end", 0))
        if w.get("kind") == "weekly":
            lt = self.local(t)
            minute, day = lt.hour * 60 + lt.minute, lt.weekday()
            a, b = _hm(w["from"]), _hm(w["to"])
            if a < b:
                return day in w["days"] and a <= minute < b
            return (day in w["days"] and minute >= a) or ((day - 1) % 7 in w["days"] and minute < b)
        return False

    def maintenance_at(self, t: Optional[float] = None, device: Optional[str] = None) -> Optional[dict]:
        """The maintenance window that covers time ``t`` (and the device, when given), or None. For incident handling:
        an outage inside a window is maintenance, not a fault."""
        t = self.clock() if t is None else t
        for w in self.maint_list():
            if not self.window_active(w, t):
                continue
            if not w.get("devices") or (device and device in w["devices"]):
                return w
        return None

    def window_view(self, w: dict) -> dict:
        out = dict(w)
        out["active"] = self.window_active(w, self.clock())
        out["past"] = w.get("kind") == "once" and float(w.get("end", 0)) <= self.clock()
        return out

    # ------------------------------------------------------------------ state
    def _state(self) -> dict:
        st = self.store.get_setting("notify_state", {}) or {}
        for k in ("pending", "open", "cooldown", "sent", "bucket", "once"):
            st.setdefault(k, {})
        return st

    def _save(self, st: dict) -> None:
        self.store.set_setting("notify_state", st)

    def _log(self, t, eid, key, sev, data, outcome, maint=None) -> int:
        cur = self.store.execute("INSERT INTO notify_log(t, event, key, severity, data, outcome, maint) VALUES(?,?,?,?,?,?,?)",
                                 (t, eid, key, sev, json.dumps(data, separators=(",", ":")), json.dumps(outcome), maint))
        return cur.lastrowid

    def _outcome(self, occ: int, changes: dict, data_patch: Optional[dict] = None) -> None:
        row = self.store.one("SELECT outcome, data FROM notify_log WHERE id=?", (occ,))
        if not row:
            return
        out = json.loads(row["outcome"] or "{}")
        out.update({str(k): v for k, v in changes.items()})
        args = [json.dumps(out)]
        sql = "UPDATE notify_log SET outcome=?"
        if data_patch:
            data = json.loads(row["data"] or "{}")
            data.update(data_patch)
            sql += ", data=?"
            args.append(json.dumps(data, separators=(",", ":")))
        self.store.execute(sql + " WHERE id=?", tuple(args) + (occ,))

    # ------------------------------------------------------------------ announcing
    def emit(self, eid: str, key: str, data: Optional[dict] = None, severity: Optional[str] = None, t: Optional[float] = None,
             resolve: bool = False, device: Optional[str] = None, once: bool = False) -> Optional[int]:
        """Announce an event (``resolve=True``: the condition behind ``key`` cleared). Returns the occurrence id.

        ``once``: an occurrence with the same key is announced only once (until the history forgets it)."""
        e = ev_mod.get(eid)
        if e is None:
            log.warning("notify: unknown event %s", eid)
            return None
        t = self.clock() if t is None else float(t)
        data = dict(data or {})
        if device:
            data["device"] = device
        with self._lock:
            st = self._state()
            if resolve:
                self._resolve(st, e, key, t)
                self._save(st)
                self._tap(eid, key, data, True, t)
                return None
            if once:
                if key in st["once"]:
                    return None
                st["once"][key] = t
            sev = severity if severity in SEVERITIES else e.severity
            if key in st["pending"] or key in st["open"]:
                self._save(st)
                return None                                   # the same condition is already being announced
            self._tap(eid, key, dict(data, severity=sev), False, t)
            w = self.maintenance_at(t, data.get("device"))
            if w:
                occ = self._log(t, eid, key, sev, data, {"*": "maintenance"}, w["id"])
                self._save(st)
                return occ
            if e.hold:
                occ = self._log(t, eid, key, sev, data, {"*": "pending"})
                st["pending"][key] = {"occ": occ, "t": t, "event": eid, "sev": sev}
                self._save(st)
                return occ
            occ = self._log(t, eid, key, sev, data, {})
            self._dispatch(st, e, key, sev, data, occ, t)
            self._save(st)
            return occ

    def _tap(self, eid: str, key: str, data: dict, resolve: bool, t: float) -> None:
        for fn in list(self.taps):
            try:
                fn(eid, key, data, resolve, t)
            except Exception:
                log.exception("notify: tap failed (%s)", eid)

    def _resolve(self, st: dict, e: Event, key: str, t: float) -> None:
        p = st["pending"].pop(key, None)
        if p:
            self._outcome(p["occ"], {"*": "flap"}, {"resolved": t})
            return
        o = st["open"].pop(key, None)
        if not o:
            return                                           # never announced: no all-clear
        self._outcome(o["occ"], {}, {"resolved": t})
        row = self.store.one("SELECT data, severity FROM notify_log WHERE id=?", (o["occ"],))
        data = json.loads(row["data"]) if row else {}
        users = {u["id"]: u for u in self.users()}
        for uid in o.get("told") or []:
            u = users.get(int(uid))
            if not u:
                continue
            prefs = self.prefs(u["id"])
            if self.quiet_now(prefs, t):
                continue                                     # the nightly report tells the rest
            chans = self.usable(u["id"], prefs["rules"][e.id]["channels"], prefs)
            if chans:
                msg = self.message(e, data, row["severity"] if row else e.severity, u, back=t - o["t"])
                self._queue(u, chans, msg, None, t=t, same="back:%d" % o["occ"])

    def _dispatch(self, st: dict, e: Event, key: str, sev: str, data: dict, occ: int, t: float) -> None:
        cfg = self.settings()
        changes, told = {}, []
        for u in self.users():
            uid = u["id"]
            if e.personal and int(data.get("user_id") or -1) != uid:
                continue
            prefs = self.prefs(uid)
            rule = prefs["rules"][e.id]
            if rule["mode"] == "off":
                continue
            if rule["mode"] == "nightly" or RANK[sev] < RANK[rule["min"]]:
                changes[uid] = "nightly"
                continue
            chans = self.usable(uid, rule["channels"], prefs)
            if not chans:
                changes[uid] = "nochannel"
                continue
            ck = "%d|%s" % (uid, key)
            if st["cooldown"].get(ck, 0) > t:
                changes[uid] = "cooldown"
                continue
            if self.quiet_now(prefs, t) and not (rule["mode"] == "immediate" and sev == "critical"):
                changes[uid] = "quiet"
                continue
            if rule["mode"] == "hourly":
                st["bucket"].setdefault(str(uid), []).append(occ)
                changes[uid] = "bundled"
                told.append(uid)
                st["cooldown"][ck] = t + cfg["cooldown_h"] * 3600
                continue
            if not self._room(st, uid, t, u):
                changes[uid] = "capped"
                continue
            changes[uid] = "sending"
            told.append(uid)
            st["cooldown"][ck] = t + cfg["cooldown_h"] * 3600
            self._queue(u, chans, self.message(e, data, sev, u), occ, t=t)
        if not changes:
            changes = {"*": "nightly"}
        self._outcome(occ, changes)
        if e.recovery and told:
            st["open"][key] = {"occ": occ, "t": t, "told": told}

    def _room(self, st: dict, uid: int, t: float, u: dict) -> bool:
        """Count one message against the daily limit; False (and once a day a note) when the limit is reached."""
        day = self.local(t).date().isoformat()
        rec = st["sent"].get(str(uid))
        if not rec or rec[0] != day:
            rec = [day, 0, False]
        if rec[1] >= self.settings()["daily_cap"]:
            if not rec[2]:
                rec[2] = True
                prefs = self.prefs(uid)
                chans = sorted({c for r in prefs["rules"].values() if r["mode"] in ("immediate", "hourly") for c in r["channels"]})
                chans = self.usable(uid, chans, prefs)
                if chans:
                    r = self.renderer(u)
                    self._queue(u, chans, {"title": r.tr("notify.cap.title"), "text": r.tr("notify.cap.text", n=rec[1]),
                                           "severity": "info", "tag": "cap", "url": self.link("#/reports")}, None, t=t)
            st["sent"][str(uid)] = rec
            return False
        rec[1] += 1
        st["sent"][str(uid)] = rec
        return True

    # ------------------------------------------------------------------ channels
    def push_subs(self, user_id: Optional[int] = None) -> list:
        if user_id is None:
            return self.people.query("SELECT id, user_id, created, label, last_ok, last_err, fails FROM push_subs ORDER BY id")
        return self.people.query("SELECT id, user_id, created, label, last_ok, last_err, fails FROM push_subs WHERE user_id=? ORDER BY id",
                                (int(user_id),))

    def shared_ready(self, ch: str, prefs: Optional[dict] = None) -> bool:
        reports = self.channels
        if reports is None:
            return False
        from ..nightly.deliver import missing
        cfg = dict(reports.settings()["channels"][ch])
        if ch == "email" and prefs and prefs.get("email"):
            cfg["to"] = prefs["email"]
        return not missing(ch, cfg, reports._secrets(ch))

    def channel_state(self, user_id: int) -> dict:
        prefs = self.prefs(user_id)
        out = {"webpush": {"usable": bool(self.push_subs(user_id)) and self.vapid_public() is not None,
                           "devices": len(self.push_subs(user_id))}}
        for ch in SHARED:
            out[ch] = {"usable": self.shared_ready(ch, prefs)}
        return out

    def usable(self, user_id: int, chans: list, prefs: Optional[dict] = None) -> list:
        out = []
        for ch in chans:
            if ch == "webpush":
                if self.push_subs(user_id):
                    out.append(ch)
            elif ch in SHARED and self.shared_ready(ch, prefs):
                out.append(ch)
        return out

    # ------------------------------------------------------------------ messages
    def renderer(self, u: dict):
        from ..nightly.render import Renderer
        lang = (u or {}).get("lang") or self.people.get_setting("language", None) or "en"
        return Renderer(lang, self.tz(), self.local(self.clock()).date().isoformat())

    def link(self, frag: str = "") -> str:
        reports = self.channels
        base = reports.settings().get("link") if reports else ""
        return ("%s/%s" % (base, frag)) if base else ""

    def message(self, e: Event, data: dict, sev: str, u: dict, back: Optional[float] = None) -> dict:
        r = self.renderer(u)
        vars_ = {k: v for k, v in data.items() if isinstance(v, (str, int, float)) and k not in ("user_id",)}
        if e.id == "security_notice":
            kind = str(data.get("kind") or "")
            nv = r.values({k: v for k, v in data.items() if k in ("ip", "agent", "left", "version", "user")})
            nv["when"] = r.values({"x_at": data.get("t") or self.clock()})["x"]
            text = "%s %s" % (r.tr("notice.title." + kind), r.tr("notice.text." + kind, **nv))
        elif back is not None:
            text = r.tr("notify.back.%s" % e.id, **r.values(dict(vars_, after_dur=back)))
        else:
            text = r.tr("notify.msg.%s" % e.id, **r.values(vars_))
            if e.id == "dev_offline" and isinstance(data.get("affected"), int) and data["affected"] > 0:
                text += " " + r.tr("notify.msg.dev_offline_chain", n=data["affected"], autos=int(data.get("autos") or 0))
        title = r.tr("notify.back_title", what=r.tr(e.title)) if back is not None else r.tr(e.title)
        frag = "#/devices/%s" % data["device"] if data.get("device") else "#/settings/notifications"
        site = self.site_title()
        return {"title": "Lanloom · " + (site + " · " if site else "") + title, "text": text, "severity": "info" if back is not None else sev,
                "tag": e.id, "url": self.link(frag), "event": e.id}

    def _queue(self, u: dict, chans: list, msg: dict, occ: Optional[int], t: float, same: Optional[str] = None) -> None:
        """``same``: a mark shared by copies of one message to several people (a shared channel sends it once)."""
        self._outbox.append({"user": u, "channels": chans, "msg": msg, "occ": occ, "t": t,
                             "same": same or ("occ:%d" % occ if occ else None)})

    # ------------------------------------------------------------------ delivery
    def deliver_one(self, ch: str, u: dict, msg: dict) -> tuple:
        if self.core.demo:
            return True, "report.send.ok"                   # the demo never sends anything out
        if self.sender is not None:
            return self.sender(ch, u, msg)
        if ch == "webpush":
            return self._send_push(u["id"], msg)
        reports = self.channels
        from ..nightly import deliver as dl
        cfg = dict(reports.settings()["channels"][ch])
        prefs = self.prefs(u["id"])
        if ch == "email" and prefs.get("email"):
            cfg["to"] = prefs["email"]
        full = {"kind": "notification", "title": msg["title"], "headline": msg["title"], "lines": [msg["text"]],
                "text": msg["text"] + (("\n" + msg["url"]) if msg.get("url") else ""), "url": msg.get("url") or "", "points": 0,
                "day": "", "event": msg.get("event") or "", "severity": msg.get("severity") or "info", "time": self.clock()}
        return dl.send(ch, cfg, reports._secrets(ch), full)

    def _send_push(self, user_id: int, msg: dict) -> tuple:
        from . import webpush
        pem = self._vapid_private()
        subs = self.push_subs(user_id)
        if not pem or not subs:
            return False, "notify.err.push_none"
        ok_any, last = False, "report.send.error"
        payload = {"title": msg["title"], "body": msg["text"], "url": msg.get("url") or "", "tag": msg.get("tag") or "lanloom",
                   "severity": msg.get("severity") or "info"}
        for s in subs:
            raw = self.pvault.get("push:%d" % s["id"]) if self.pvault else None
            if not raw:
                continue
            ok, key = webpush.send(json.loads(raw), payload, pem, urgency="high" if msg.get("severity") == "critical" else "normal")
            if ok:
                ok_any = True
                self.people.execute("UPDATE push_subs SET last_ok=?, last_err='', fails=0 WHERE id=?", (self.clock(), s["id"]))
            elif key == "notify.push.gone" or s["fails"] >= 20:
                self.remove_push(s["id"])
            else:
                self.people.execute("UPDATE push_subs SET last_err=?, fails=fails+1 WHERE id=?", (key, s["id"]))
            last = key
        return (True, "report.send.ok") if ok_any else (False, last)

    def drain(self) -> int:
        """Send everything queued (blocking; called in a worker thread). Shared channels get one copy per text."""
        n, done = 0, {}
        while self._outbox:
            job = self._outbox.popleft()
            results = []
            for ch in job["channels"]:
                key = (ch, job["same"], job["msg"]["text"]) if ch in ("webhook", "ntfy", "chatbot") and job.get("same") else None
                if key and key in done:
                    results.append(done[key])
                    continue
                res = self.deliver_one(ch, job["user"], job["msg"])
                if not res[0]:
                    log.info("notify: delivery via %s failed (%s)", ch, res[1])
                if key:
                    done[key] = res
                results.append(res)
            n += 1
            for occ in job.get("occs") or ([job["occ"]] if job["occ"] else []):
                ok = any(r[0] for r in results)
                self._outcome(occ, {job["user"]["id"]: "sent" if ok else "failed"},
                              None if ok else {"fail_key": next((r[1] for r in results if not r[0]), "")})
        return n

    # ------------------------------------------------------------------ rounds
    def tick(self, now: Optional[float] = None) -> None:
        """One round: due holds, the hourly bundles, periodic checks. Called every few seconds by the loop."""
        now = self.clock() if now is None else now
        with self._lock:
            st = self._state()
            hold_s = self.settings()["hold_rounds"] * ROUND_S
            for key, p in list(st["pending"].items()):
                if now - p["t"] < hold_s:
                    continue
                st["pending"].pop(key)
                e = ev_mod.get(p["event"])
                row = self.store.one("SELECT data FROM notify_log WHERE id=?", (p["occ"],))
                data = json.loads(row["data"]) if row else {}
                w = self.maintenance_at(now, data.get("device"))
                if e is None or w:
                    self._outcome(p["occ"], {"*": "maintenance"} if w else {"*": "nightly"})
                    if w:
                        self.store.execute("UPDATE notify_log SET maint=? WHERE id=?", (w["id"], p["occ"]))
                    continue
                self.store.execute("UPDATE notify_log SET outcome='{}' WHERE id=?", (p["occ"],))
                self._dispatch(st, e, key, p["sev"], data, p["occ"], now)
            self._flush_bundles(st, now)
            for ck, until in list(st["cooldown"].items()):
                if until <= now:
                    st["cooldown"].pop(ck)
            for k, t0 in list(st["once"].items()):
                if now - t0 > 400 * 86400:
                    st["once"].pop(k)
            self._save(st)

    def _flush_bundles(self, st: dict, now: float) -> None:
        hour = self.local(now).strftime("%Y-%m-%dT%H")
        if st.get("flushed") == hour:
            return
        st["flushed"] = hour
        users = {u["id"]: u for u in self.users()}
        for uid_s, occs in list(st["bucket"].items()):
            st["bucket"].pop(uid_s)
            u = users.get(int(uid_s))
            if not u or not occs:
                continue
            prefs = self.prefs(u["id"])
            marks = ",".join("?" * len(occs))
            rows = self.store.query("SELECT id, event, severity, data FROM notify_log WHERE id IN (%s) ORDER BY t" % marks, tuple(occs))
            if self.quiet_now(prefs, now):
                for r in rows:
                    self._outcome(r["id"], {u["id"]: "quiet"})
                continue
            if not self._room(st, u["id"], now, u):
                for r in rows:
                    self._outcome(r["id"], {u["id"]: "capped"})
                continue
            chans = sorted({c for r in rows for c in prefs["rules"].get(r["event"], {}).get("channels", [])}, key=CHANNELS.index)
            chans = self.usable(u["id"], chans, prefs)
            if not chans:
                for r in rows:
                    self._outcome(r["id"], {u["id"]: "nochannel"})
                continue
            rd = self.renderer(u)
            lines = []
            worst = "info"
            for r in rows:
                e = ev_mod.get(r["event"])
                if e is None:
                    continue
                m = self.message(e, json.loads(r["data"] or "{}"), r["severity"], u)
                lines.append("• " + m["text"])
                if RANK[r["severity"]] > RANK[worst]:
                    worst = r["severity"]
            if not lines:
                continue
            msg = {"title": "Lanloom · " + rd.tr("notify.bundle.title", n=len(lines)), "text": "\n".join(lines[:12]) +
                   ("\n" + rd.tr("report.mail.more", n=len(lines) - 12) if len(lines) > 12 else ""),
                   "severity": worst, "tag": "bundle", "url": self.link("#/settings/notifications"), "event": "bundle"}
            self._outbox.append({"user": u, "channels": chans, "msg": msg, "occ": None, "occs": [r["id"] for r in rows], "t": now})

    def periodic(self, now: Optional[float] = None) -> None:
        """Checks that have no journal event: backups too old, a new Lanloom version (every 10 minutes)."""
        now = self.clock() if now is None else now
        if now - self._last_periodic < 600:
            return
        self._last_periodic = now
        if self.core.demo:
            return
        b = getattr(self.core, "backups", None)
        try:
            if b is not None and b.schedule().get("enabled") and b.stale(now):
                newest = b.newest()
                self.emit("backup_old", "backup:%d" % int(newest or 0), {"age_dur": int(now - newest) if newest else 0,
                                                                          "none": 0 if newest else 1}, t=now, once=True)
        except Exception:
            log.exception("notify: backup check failed")
        try:
            upd = self.core.reports._update() if getattr(self.core, "reports", None) else {}
            if upd.get("available") and upd.get("latest"):
                self.emit("update_available", "update:%s" % upd["latest"], {"version": upd["latest"], "current": __version__},
                          t=now, once=True)
            st = upd.get("status") or {}
            if st.get("state") in ("failed", "rolled_back") and st.get("version") and now - float(st.get("t") or 0) < 3600:
                self.emit("update_failed", "updfail:%s" % (st.get("nonce") or st.get("t")),
                          {"version": st["version"], "reason_key": st.get("message_key") or "update.err.internal"},
                          t=now, once=True)
        except Exception:
            log.exception("notify: update check failed")

    def on_certs(self, certs: list, now: Optional[float] = None) -> None:
        """Certificates checked by the nightly report: announce those expiring within 14 days (once a day each)."""
        now = self.clock() if now is None else now
        day = self.local(now).date().isoformat()
        for c in certs or []:
            days = c.get("days")
            if days is None or days >= 14:
                continue
            self.emit("cert_expiring", "cert:%s:%s" % (c.get("id"), day), {"name": c.get("name") or "", "days": int(days)},
                      severity="critical" if days < 0 else None, t=now, once=True)

    # ------------------------------------------------------------------ journal bridge
    def _chain_of(self, device_id) -> dict:
        """What probably hangs on a device that just went offline (failure chain, lanloom/failure.py): {"affected", "autos"}."""
        fl = getattr(self.core, "failure", None)
        if fl is None or not device_id:
            return {}
        try:
            res = fl.chain(device_id, "down")
        except Exception:            # the chain only adds a sentence; it never stops the message
            return {}
        if not res or not res["counts"]["total"]:
            return {}
        return {"affected": res["counts"]["total"], "autos": res["counts"]["automations"]}

    def on_journal(self, kind: str, data: dict, t: float) -> None:
        """Translate rows of the nightly-report journal into events (called right after each row is written)."""
        if t < self.clock() - 600:
            return                                       # a row about the past (sample data, catch-up) is no news now
        try:
            if kind == "dev_new":
                if not self.people.get_setting("setup_done", False):
                    return                               # the first scan finds everything: that is no news
                prot = self.protected(data.get("ip") or "")
                self.emit("dev_new_protected" if prot else "dev_new", "new:%s" % data.get("id"),
                          {"title": data.get("title") or data.get("ip") or "", "ip": data.get("ip") or "",
                           "vendor": data.get("vendor") or ""}, t=t, device=data.get("id"))
            elif kind in ("dev_offline", "dev_back"):
                from ..nightly.journal import INFRA_KINDS
                sev = "warning" if data.get("kind") in INFRA_KINDS else "info"
                info = {"title": data.get("title") or ""}
                if kind == "dev_offline":
                    info.update(self._chain_of(data.get("id")))
                self.emit("dev_offline", "off:%s" % data.get("id"), info, severity=sev, t=t,
                          resolve=kind == "dev_back", device=data.get("id"))
            elif kind in ("wan_down", "wan_up"):
                self.emit("wan_down", "wan", {"name": data.get("name") or ""}, t=t, resolve=kind == "wan_up")
            elif kind in ("src_fail", "src_ok"):
                from ..nightly.contrib import _refused
                if kind == "src_ok":
                    self.emit("integ_auth", "auth:%s" % data.get("id"), t=t, resolve=True)
                    self.emit("src_fail", "src:%s" % data.get("id"), t=t, resolve=True)
                elif _refused(data.get("key")):
                    self.emit("src_fail", "src:%s" % data.get("id"), t=t, resolve=True)
                    self.emit("integ_auth", "auth:%s" % data.get("id"), {"name": data.get("name") or ""}, t=t)
                else:
                    self.emit("integ_auth", "auth:%s" % data.get("id"), t=t, resolve=True)
                    self.emit("src_fail", "src:%s" % data.get("id"), {"name": data.get("name") or ""}, t=t)
        except Exception:
            log.exception("notify: journal row %s not handled", kind)

    def on_notice(self, user_id: int, kind: str, data: dict) -> None:
        try:
            now = self.clock()
            safe = {k: v for k, v in (data or {}).items() if k in ("ip", "agent", "left", "version", "user") and isinstance(v, (str, int))}
            self.emit("security_notice", "sec:%d:%s:%d" % (int(user_id), kind, int(now * 1000)),
                      dict(safe, user_id=int(user_id), kind=kind, t=now), t=now)
        except Exception:
            log.exception("notify: security notice not handled")

    # ------------------------------------------------------------------ web push keys and subscriptions
    def vapid_public(self, create: bool = False) -> Optional[str]:
        pub = self.people.get_setting("notify_vapid_public", None)
        if pub and self.pvault and self.pvault.has("notify:vapid"):
            return pub
        if not create or not self.pvault:
            return None
        from . import webpush
        pem, pub = webpush.new_vapid()
        self.pvault.put("notify:vapid", pem)
        self.people.set_setting("notify_vapid_public", pub)
        return pub

    def _vapid_private(self) -> Optional[str]:
        return self.pvault.get("notify:vapid") if self.pvault else None

    def add_push(self, user_id: int, sub: dict, label: str = "") -> dict:
        from . import webpush
        if self.vapid_public(create=True) is None:
            raise ValueError("notify.err.push_unavailable")
        clean = webpush.clean_subscription(sub)
        for s in self.push_subs(user_id):                # the same browser again: replace, never duplicate
            raw = self.pvault.get("push:%d" % s["id"])
            if raw and json.loads(raw).get("endpoint") == clean["endpoint"]:
                self.remove_push(s["id"])
        if len(self.push_subs(user_id)) >= MAX_PUSH_PER_USER:
            raise ValueError("notify.err.push_many")
        cur = self.people.execute("INSERT INTO push_subs(user_id, created, label) VALUES(?,?,?)",
                                 (int(user_id), self.clock(), str(label or "")[:80]))
        sid = cur.lastrowid
        self.pvault.put("push:%d" % sid, json.dumps(clean))
        return {"id": sid, "label": str(label or "")[:80], "created": self.clock()}

    def remove_push(self, sub_id: int, user_id: Optional[int] = None) -> bool:
        if user_id is not None:
            row = self.people.one("SELECT user_id FROM push_subs WHERE id=?", (int(sub_id),))
            if not row or row["user_id"] != int(user_id):
                return False
        n = self.people.execute("DELETE FROM push_subs WHERE id=?", (int(sub_id),)).rowcount
        if self.pvault:
            self.pvault.delete("push:%d" % int(sub_id))
        return n > 0

    def push_owner_endpoint(self, user_id: int, endpoint: str) -> Optional[int]:
        for s in self.push_subs(user_id):
            raw = self.pvault.get("push:%d" % s["id"]) if self.pvault else None
            if raw and json.loads(raw).get("endpoint") == endpoint:
                return s["id"]
        return None

    # ------------------------------------------------------------------ test message
    def test(self, u: dict, ch: str) -> dict:
        if ch not in CHANNELS:
            raise ValueError("notify.err.channel")
        now = self.clock()
        if now - self._last_test.get(u["id"], 0) < TEST_GAP_S:
            raise ValueError("notify.err.test_wait")
        self._last_test[u["id"]] = now
        if not self.usable(u["id"], [ch], self.prefs(u["id"])):
            return {"ok": False, "key": "notify.err.push_none" if ch == "webpush" else "report.err.channel_incomplete"}
        r = self.renderer(u)
        ok, key = self.deliver_one(ch, u, {"title": "Lanloom · " + r.tr("notify.test.title"), "text": r.tr("notify.test.text"),
                                           "severity": "info", "tag": "test", "url": self.link("#/settings/notifications"),
                                           "event": "test"})
        return {"ok": ok, "key": key}

    # ------------------------------------------------------------------ heartbeat (dead man's switch)
    def heartbeat(self) -> dict:
        raw = self.people.get_setting("notify_heartbeat", {}) or {}
        return {"enabled": bool(raw.get("enabled")), "every_min": raw.get("every_min") if raw.get("every_min") in HB_CHOICES else 5,
                "url_set": bool(self.pvault and self.pvault.has("notify:heartbeat:url")),
                "last_t": raw.get("last_t"), "ok": raw.get("ok"), "key": raw.get("key") or "", "fails": int(raw.get("fails") or 0),
                "last_ok": raw.get("last_ok")}

    def update_heartbeat(self, patch: dict) -> dict:
        raw = self.people.get_setting("notify_heartbeat", {}) or {}
        if "url" in patch:
            url = str(patch["url"] or "").strip()
            if url and (not URL_RE.match(url) or len(url) > 500):
                raise ValueError("report.err.url")
            if self.pvault:
                if url:
                    self.pvault.put("notify:heartbeat:url", url)
                else:
                    self.pvault.delete("notify:heartbeat:url")
        if "every_min" in patch:
            if patch["every_min"] not in HB_CHOICES or isinstance(patch["every_min"], bool):
                raise ValueError("notify.err.choice")
            raw["every_min"] = patch["every_min"]
        if "enabled" in patch:
            raw["enabled"] = bool(patch["enabled"])
        if raw.get("enabled") and not (self.pvault and self.pvault.has("notify:heartbeat:url")):
            raise ValueError("notify.err.heartbeat_url")
        self.people.set_setting("notify_heartbeat", raw)
        self._hb_next = 0.0
        return self.heartbeat()

    def beat(self, now: Optional[float] = None) -> dict:
        """Call the heartbeat address once (blocking). Never raises; the result is stored."""
        now = self.clock() if now is None else now
        url = self.pvault.get("notify:heartbeat:url") if self.pvault else None
        if not url:
            return {"ok": False, "key": "notify.err.heartbeat_url"}
        try:
            if self.heartbeat_open is not None:
                self.heartbeat_open(url)
            else:
                req = urllib.request.Request(url, method="GET", headers={"User-Agent": "lanloom/%s" % __version__})
                with urllib.request.urlopen(req, timeout=HB_TIMEOUT, context=ssl.create_default_context()) as resp:
                    resp.read(1024)
            ok, key = True, "report.send.ok"
        except urllib.error.HTTPError as exc:
            ok, key = False, "report.send.auth" if exc.code in (401, 403) else "report.send.rejected"
        except (ssl.SSLError, ssl.CertificateError):
            ok, key = False, "report.send.tls"
        except (socket.timeout, TimeoutError):
            ok, key = False, "report.send.timeout"
        except (urllib.error.URLError, OSError):
            ok, key = False, "report.send.unreachable"
        except Exception:
            ok, key = False, "report.send.error"
        raw = self.people.get_setting("notify_heartbeat", {}) or {}
        raw.update(last_t=now, ok=ok, key=key, fails=0 if ok else int(raw.get("fails") or 0) + 1)
        if ok:
            raw["last_ok"] = now
        self.people.set_setting("notify_heartbeat", raw)
        if not ok:
            log.info("heartbeat: call failed (%s)", key)
        return {"ok": ok, "key": key}

    def beat_due(self, now: float) -> bool:
        hb = self.heartbeat()
        if not hb["enabled"] or not hb["url_set"] or self.core.demo or getattr(self.core, "remote", False):
            return False                                 # one heartbeat per instance: the home site sends it
        if now < self._hb_next:
            return False
        self._hb_next = now + hb["every_min"] * 60
        return True

    # ------------------------------------------------------------------ history
    def history(self, user: dict, limit: int = 100, before: Optional[int] = None) -> list:
        limit = max(1, min(int(limit or 100), 500))
        rows = self.store.query("SELECT id, t, event, key, severity, data, outcome, maint FROM notify_log %s ORDER BY id DESC LIMIT ?"
                                % ("WHERE id < ?" if before else ""), ((int(before),) if before else ()) + (limit * 3,))
        admin = user.get("role") == "admin"
        names = {u["id"]: u["username"] for u in self.users()} if admin else {}
        out = []
        for r in rows:
            data = json.loads(r["data"] or "{}")
            outcome = json.loads(r["outcome"] or "{}")
            e = ev_mod.get(r["event"])
            if e and e.personal and int(data.get("user_id") or -1) != user["id"] and not admin:
                continue
            mine = outcome.get(str(user["id"])) or outcome.get("*") or ("nightly" if not outcome else "nightly")
            if mine == "sending":
                mine = "pending"
            entry = {"id": r["id"], "t": r["t"], "event": r["event"], "severity": r["severity"], "status": mine,
                     "maint": r["maint"], "resolved": data.get("resolved"),
                     "vars": {k: v for k, v in data.items() if k in ("title", "ip", "name", "version", "days", "vendor")},
                     "device": data.get("device")}
            if e and e.id == "security_notice":
                entry["vars"] = {"kind": data.get("kind")}
            if admin:
                entry["outcome"] = {(names.get(int(k), "?") if k.isdigit() else k): v for k, v in outcome.items()}
            out.append(entry)
            if len(out) >= limit:
                break
        return out

    def summary(self, since: float, until: float) -> dict:
        """Counts for the nightly report: what was sent, held, suppressed, and failed deliveries."""
        rows = self.store.query("SELECT event, outcome, maint FROM notify_log WHERE t > ? AND t <= ?", (since, until))
        count = {s: 0 for s in STATUSES}
        maint = set()
        for r in rows:
            outcome = json.loads(r["outcome"] or "{}")
            for v in set(outcome.values()):
                if v in count:
                    count[v] += 1
            if r["maint"]:
                maint.add(r["maint"])
        names = {w["id"]: w.get("title") or "" for w in self.maint_list()}
        return {"n": len(rows), "count": count, "maint_names": [names.get(w, "") for w in sorted(maint)]}

    def prune(self) -> None:
        now = self.clock()
        self.store.execute("DELETE FROM notify_log WHERE t < ?", (now - KEEP_LOG_DAYS * 86400,))
        self.store.execute("DELETE FROM notify_log WHERE id NOT IN (SELECT id FROM notify_log ORDER BY id DESC LIMIT ?)", (KEEP_LOG_ROWS,))
        ws = self.maint_list()
        keep = [w for w in ws if not (w.get("kind") == "once" and float(w.get("end", 0)) < now - 30 * 86400)]
        if len(keep) != len(ws):
            self.store.set_setting("notify_maint", keep)
        if self.vault:                                   # subscriptions of deleted accounts went with the account
            have = {r["id"] for r in self.store.query("SELECT id FROM push_subs")}
            for r in self.store.query("SELECT name FROM secrets WHERE name LIKE 'push:%'"):
                sid = r["name"][5:]
                if not sid.isdigit() or int(sid) not in have:
                    self.vault.delete(r["name"])

    # ------------------------------------------------------------------ loop
    async def loop(self) -> None:
        last_prune = 0.0
        while True:
            now = self.clock()
            try:
                if now >= self._fail_until:
                    self.tick(now)
                    self.periodic(now)
                    if self._outbox:
                        await asyncio.to_thread(self.drain)
                    if self.beat_due(now):
                        await asyncio.to_thread(self.beat, now)
                    if now - last_prune > 6 * 3600:
                        last_prune = now
                        self.prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("notify: round failed (retrying in 5 minutes)")
                self._fail_until = now + 300
            await asyncio.sleep(5)

from . import report as _report  # noqa: E402,F401 - registers the nightly report section and the start-page note
