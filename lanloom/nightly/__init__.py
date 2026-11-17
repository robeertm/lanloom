"""Nightly report: every morning a plain summary of what happened in the network overnight.

* Rule-based, from Lanloom's own data (no AI, no online service). Sections come from the contribution register
  (``contrib.py``); a section appears only when its source exists.
* Schedule: every day at ``time`` (default 06:30) in the report time zone, over the time since the previous report.
  The report carries the date of that morning. When Lanloom was not running at report time, the report is made at
  the next start (and says so in one quiet line); several missed mornings become one report.
* Archive: ``keep_days`` (default 30). Points that need a look can be ticked off; the state is stored.
* Delivery (optional, everything off by default): e-mail, webhook, push topic, chat bot. Secrets live encrypted in the
  secret store; nothing leaves the host before a channel is set up. A failed delivery is retried quietly.
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import __version__
from . import contrib
from .contrib import REGISTRY, Contribution, Ctx, register  # noqa: F401 - public register for later features
from . import hubsection  # noqa: F401,E402 - registers the hub bridge section
from . import radiosection  # noqa: F401,E402 - registers the radio and batteries section of the hub bridge
from .journal import Journal, Watch

log = logging.getLogger("lanloom")

KEEP_CHOICES = (7, 14, 30, 90, 365)
CHANNELS = ("email", "webhook", "ntfy", "chatbot")
SECRET_FIELDS = {"email": ("password",), "webhook": ("url",), "ntfy": ("topic", "token"), "chatbot": ("token",)}
DEFAULTS = {
    "time": "06:30",
    "tz": "",                 # "" = automatic (TZ of the container, else the browser's zone)
    "keep_days": 30,
    "link": "",               # address of this Lanloom for the link in deliveries (optional)
    "channels": {
        "email": {"enabled": False, "host": "", "port": 587, "security": "starttls", "username": "", "sender": "", "to": ""},
        "webhook": {"enabled": False},
        "ntfy": {"enabled": False, "server": ""},
        "chatbot": {"enabled": False, "chat_id": ""},
    },
}
PLAIN_WAIT_S = 240           # the AI paragraph may delay the delivery this long at most
CATCH_UP_AFTER_S = 600       # a report made this much later than planned says "made at the next start"
WATCH_EVERY_S = 60
LOOP_EVERY_S = 30
DELIVERY_RETRIES = 3
DELIVERY_RETRY_S = 900
DAY_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
TIME_RE = re.compile(r"\A([01]\d|2[0-3]):([0-5]\d)\Z")


def zone(name: str):
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


class Reports:
    def __init__(self, core, data_dir=None, clock: Callable[[], float] = time.time):
        self.core = core
        self.store = core.store
        self.vault = core.vault
        self.clock = clock
        self.data_dir = data_dir
        self.plain_hook = None          # AI: report -> {"text", ...} or None (set by the app when AI exists)
        self.plain_delivery = lambda: False     # AI: may the paragraph go into outside deliveries?
        self.night_hook = None          # AI: nightly identification suggestions (only when switched on)
        self.journal = Journal(core.store, clock)
        self.watch = Watch(core, self.journal, clock)
        self.update_state: Callable[[], dict] = lambda: {}
        self.cert_check: Optional[Callable] = None          # replaced in tests
        self.on_certs: Optional[Callable] = None            # notification rules: certificates checked for a report
        self._lock = asyncio.Lock()
        self._fail_until = 0.0

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        raw = self.store.get_setting("report_cfg", {}) or {}
        out = json.loads(json.dumps(DEFAULTS))
        for k in ("time", "tz", "keep_days", "link"):
            if k in raw:
                out[k] = raw[k]
        for ch in CHANNELS:
            out["channels"][ch].update((raw.get("channels") or {}).get(ch) or {})
        return out

    def public_settings(self) -> dict:
        s = self.settings()
        for ch, fields in SECRET_FIELDS.items():
            s["channels"][ch]["secrets"] = {f: bool(self.vault and self.vault.has("report:%s:%s" % (ch, f))) for f in fields}
        s["tz_effective"] = self.tz_name()
        s["tz_auto"] = self.store.get_setting("report_tz_auto", "") or ""
        s["next_at"] = self.next_at()
        return s

    def update_settings(self, patch: dict, secrets: Optional[dict] = None) -> dict:
        cur = self.settings()
        if "time" in patch:
            if not isinstance(patch["time"], str) or not TIME_RE.match(patch["time"]):
                raise ValueError("report.err.time")
            cur["time"] = patch["time"]
        if "tz" in patch:
            tz = str(patch["tz"] or "").strip()
            if tz and zone(tz) is None:
                raise ValueError("report.err.tz")
            cur["tz"] = tz
        if "keep_days" in patch:
            if patch["keep_days"] not in KEEP_CHOICES:
                raise ValueError("report.err.keep")
            cur["keep_days"] = patch["keep_days"]
        if "link" in patch:
            link = str(patch["link"] or "").strip().rstrip("/")
            if link and not re.match(r"\Ahttps?://[^\s/?#]+(/[^\s?#]*)?\Z", link):
                raise ValueError("report.err.link")
            cur["link"] = link[:200]
        for ch, values in (patch.get("channels") or {}).items():
            if ch not in CHANNELS or not isinstance(values, dict):
                continue
            from .deliver import clean_channel
            cur["channels"][ch] = clean_channel(ch, dict(cur["channels"][ch], **values))
        for key, value in (secrets or {}).items():
            ch, _, field = str(key).partition(".")
            if ch in SECRET_FIELDS and field in SECRET_FIELDS[ch] and isinstance(value, str) and self.vault:
                if value == "":
                    self.vault.delete("report:%s:%s" % (ch, field))
                else:
                    from .deliver import clean_secret
                    self.vault.put("report:%s:%s" % (ch, field), clean_secret(ch, field, value))
        for ch in CHANNELS:
            if cur["channels"][ch].get("enabled"):
                from .deliver import missing
                if missing(ch, cur["channels"][ch], self._secrets(ch)):
                    raise ValueError("report.err.channel_incomplete")
        stored = {k: cur[k] for k in ("time", "tz", "keep_days", "link")}
        stored["channels"] = {ch: {k: v for k, v in cur["channels"][ch].items() if k != "secrets"} for ch in CHANNELS}
        self.store.set_setting("report_cfg", stored)
        return self.public_settings()

    def set_auto_tz(self, name: str) -> None:
        if zone(name) is not None and self.store.get_setting("report_tz_auto", "") != name:
            self.store.set_setting("report_tz_auto", name)

    def _secrets(self, ch: str) -> dict:
        out = {}
        for f in SECRET_FIELDS.get(ch, ()):
            v = self.vault.get("report:%s:%s" % (ch, f)) if self.vault else None
            if v:
                out[f] = v
        return out

    # ------------------------------------------------------------------ time
    def tz_name(self) -> str:
        s = self.settings()
        for name in (s.get("tz"), os.environ.get("TZ", "").strip(), self.store.get_setting("report_tz_auto", "")):
            if name and zone(name) is not None:
                return name
        return "UTC"

    def tz(self):
        return zone(self.tz_name()) or timezone.utc

    def planned_at(self, day: date) -> float:
        hh, mm = (int(x) for x in self.settings()["time"].split(":"))
        return datetime(day.year, day.month, day.day, hh, mm, tzinfo=self.tz()).timestamp()

    def due_day(self, now: float) -> date:
        """The morning whose report time is the latest one not after ``now``."""
        today = datetime.fromtimestamp(now, self.tz()).date()
        return today if now >= self.planned_at(today) else today - timedelta(days=1)

    def next_at(self) -> float:
        now = self.clock()
        day = self.due_day(now) + timedelta(days=1)
        return self.planned_at(day)

    def epoch(self) -> float:
        t = self.store.get_setting("report_epoch", None)
        if t is None:
            t = self.clock()
            self.store.set_setting("report_epoch", t)
        return float(t)

    # ------------------------------------------------------------------ lifecycle hooks
    def on_start(self) -> None:
        now = self.clock()
        self.epoch()
        prev = self.store.get_setting("report_last_version", None)
        beat = self.store.get_setting("report_heartbeat", None)
        data = {"version": __version__}
        if prev and prev != __version__:
            data["prev"] = prev
        if isinstance(beat, (int, float)):
            data["down_s"] = max(0, int(now - beat))
        self.journal.add("app_start", data, now)
        self.store.set_setting("report_last_version", __version__)
        self.store.set_setting("report_heartbeat", now)

    async def loop(self) -> None:
        last_watch = 0.0
        while True:
            now = self.clock()
            try:
                if now - last_watch >= WATCH_EVERY_S:
                    last_watch = now
                    if not self.core.demo:
                        self.watch.observe(now)
                    self.store.set_setting("report_heartbeat", now)
                if now >= self._fail_until:
                    await self.run_due(now)
                    await self.retry_deliveries(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("nightly report: round failed (retrying in 5 minutes)")
                self._fail_until = now + 300
            await asyncio.sleep(LOOP_EVERY_S)

    # ------------------------------------------------------------------ building
    async def run_due(self, now: Optional[float] = None) -> Optional[dict]:
        """Make the report of the latest morning when it is missing. Returns the new report or None."""
        now = self.clock() if now is None else now
        async with self._lock:
            day = self.due_day(now)
            key = day.isoformat()
            if self.store.one("SELECT day FROM reports WHERE day=?", (key,)):
                return None
            planned = self.planned_at(day)
            prev = self.latest()
            if prev is None and self.epoch() > planned:
                return None                      # installed after this morning's report time: the first one comes tomorrow
            if prev and prev["day"] > key:
                return None                      # clock or time zone went backwards: never rewrite history
            since = prev["until"] if prev else max(planned - 86400, self.epoch())
            report = await self.build(since, now, prev, key, catch_up=now - planned > CATCH_UP_AFTER_S, planned=planned)
            self.save(report)
            self.prune()
        await self.add_plain(report)
        await self.deliver(report)
        self.after_report()
        return report

    # ------------------------------------------------------------------ AI paragraph "In plain words" (optional)
    async def add_plain(self, report: dict) -> None:
        """Ask the AI for the plain-words paragraph (when one is set up and switched on). The rule-based report stays
        the truth: without an answer the paragraph is simply missing."""
        hook = self.plain_hook
        if hook is None:
            return
        try:
            plain = await asyncio.wait_for(asyncio.to_thread(hook, report), PLAIN_WAIT_S)
        except Exception:
            plain = None
        if plain and plain.get("text"):
            report["plain"] = plain
            self.set_plain(report["day"], plain)

    def set_plain(self, day: str, plain: dict) -> None:
        row = self.store.one("SELECT data FROM reports WHERE day=?", (day,))
        if not row:
            return
        data = json.loads(row["data"])
        data["plain"] = plain
        self.store.execute("UPDATE reports SET data=? WHERE day=?", (json.dumps(data, separators=(",", ":")), day))

    def after_report(self) -> None:
        hook = self.night_hook
        if hook is None:
            return
        import threading
        threading.Thread(target=self._night, args=(hook,), name="report-after", daemon=True).start()

    @staticmethod
    def _night(hook) -> None:
        try:
            hook()
        except Exception:
            log.exception("nightly report: follow-up failed")

    async def build(self, since: float, until: float, prev: Optional[dict], day: str, catch_up: bool = False,
                    planned: Optional[float] = None, preview: bool = False) -> dict:
        certs = await self.check_certs()
        if self.on_certs is not None and not preview:
            try:
                self.on_certs(certs)
            except Exception:
                log.exception("nightly report: certificate notification failed")
        return self.assemble(since, until, prev, day, certs, catch_up=catch_up, planned=planned, preview=preview)

    def assemble(self, since: float, until: float, prev: Optional[dict], day: str, certs: list, catch_up: bool = False,
                 planned: Optional[float] = None, preview: bool = False) -> dict:
        ctx = Ctx(core=self.core, journal=self.journal, since=since, until=until, prev=prev, certs=certs,
                  update=self._update(), data_dir=self.data_dir)
        sections = []
        for c in list(REGISTRY):
            try:
                if not c.available(ctx):
                    continue
                sec = c.build(ctx)
            except Exception:
                log.exception("nightly report: section %s failed", c.id)
                sec = contrib.section(c.id, c.title, "report.section_failed", [])
            if isinstance(sec, list):           # a contribution may give one section per connection
                sections.extend(x for x in sec if x)
            elif sec:
                sections.append(sec)
        points = sum(1 for s in sections for i in s["items"] if i["point"])
        level = "ok"
        for s in sections:
            if s["level"] == "danger" or (s["level"] == "warn" and level == "ok"):
                level = s["level"]
        out = {"day": day, "since": since, "until": until, "created": self.clock(), "tz": self.tz_name(), "points": points,
               "level": level, "sections": sections, "catch_up": bool(catch_up), "planned": planned, "preview": preview,
               "long": until - since > 30 * 3600, "version": __version__}
        if ctx.snapshot or (prev or {}).get("controller_snapshot"):
            out["controller_snapshot"] = ctx.snapshot or (prev or {}).get("controller_snapshot")
        if ctx.families or (prev or {}).get("family_states"):
            out["family_states"] = ctx.families or (prev or {}).get("family_states")
        return out

    def _update(self) -> dict:
        try:
            return self.update_state() or {}
        except Exception:
            return {}

    async def check_certs(self) -> list:
        if self.cert_check is not None:
            return self.cert_check()
        if self.core.demo:
            return self.core.demo.report_certs() if hasattr(self.core.demo, "report_certs") else []
        from .certs import endpoints, check_all
        eps = endpoints(self.core.integrations())
        if not eps:
            return []
        return await asyncio.to_thread(check_all, eps)

    # ------------------------------------------------------------------ archive
    def save(self, report: dict) -> None:
        self.store.execute("INSERT OR REPLACE INTO reports(day, created, since, until, data, read, points, delivery) VALUES(?,?,?,?,?,?,?,?)",
                           (report["day"], report["created"], report["since"], report["until"],
                            json.dumps(report, separators=(",", ":")), None, report["points"], "{}"))

    def prune(self) -> None:
        keep = int(self.settings()["keep_days"])
        cutoff = (datetime.fromtimestamp(self.clock(), self.tz()).date() - timedelta(days=keep)).isoformat()
        for row in self.store.query("SELECT day FROM reports WHERE day <= ?", (cutoff,)):
            self.store.execute("DELETE FROM report_marks WHERE day=?", (row["day"],))
            self.store.execute("DELETE FROM reports WHERE day=?", (row["day"],))
        availability = (self.store.get_setting("retention", {}) or {}).get("availability_days", 35)
        self.journal.prune(max(keep, int(availability) if isinstance(availability, int) else 35))

    def latest(self) -> Optional[dict]:
        row = self.store.one("SELECT data FROM reports ORDER BY day DESC LIMIT 1")
        return json.loads(row["data"]) if row else None

    def _marks(self, day: str) -> dict:
        return {r["item"]: r["done"] for r in self.store.query("SELECT item, done FROM report_marks WHERE day=?", (day,))}

    def summary_rows(self) -> list:
        out = []
        for r in self.store.query("SELECT day, created, read, points, data, delivery FROM reports ORDER BY day DESC"):
            data = json.loads(r["data"])
            marks = self._marks(r["day"])
            ids = [i["id"] for s in data["sections"] for i in s["items"] if i["point"]]
            out.append({"day": r["day"], "created": r["created"], "read": bool(r["read"]), "points": r["points"],
                        "open": sum(1 for i in ids if i not in marks), "level": data.get("level", "ok"),
                        "catch_up": bool(data.get("catch_up"))})
        return out

    def listing(self) -> dict:
        rows = self.summary_rows()
        return {"reports": rows, "unread": sum(1 for r in rows if not r["read"]), "keep_days": self.settings()["keep_days"],
                "next_at": self.next_at(), "tz": self.tz_name()}

    def unread(self) -> int:
        row = self.store.one("SELECT COUNT(*) AS n FROM reports WHERE read IS NULL")
        return row["n"] if row else 0

    def get(self, day: str) -> Optional[dict]:
        if not DAY_RE.match(day or ""):
            return None
        row = self.store.one("SELECT data, read, delivery FROM reports WHERE day=?", (day,))
        if not row:
            return None
        data = json.loads(row["data"])
        data.pop("controller_snapshot", None)
        data.pop("family_states", None)
        marks = self._marks(day)
        for s in data["sections"]:
            for i in s["items"]:
                if i["point"]:
                    i["done"] = marks.get(i["id"])
        data["read"] = bool(row["read"])
        data["open"] = sum(1 for s in data["sections"] for i in s["items"] if i["point"] and not i.get("done"))
        data["delivery"] = {ch: {"ok": v.get("ok"), "key": v.get("key"), "t": v.get("t")}
                            for ch, v in (json.loads(row["delivery"] or "{}")).items()}
        neighbours = self.store.query("SELECT day FROM reports ORDER BY day")
        days = [n["day"] for n in neighbours]
        idx = days.index(day)
        data["older"] = days[idx - 1] if idx > 0 else None
        data["newer"] = days[idx + 1] if idx + 1 < len(days) else None
        return data

    def mark_read(self, day: str, read: bool = True) -> bool:
        return self.store.execute("UPDATE reports SET read=? WHERE day=?", (self.clock() if read else None, day)).rowcount > 0

    def mark_item(self, day: str, item_id: str, done: bool) -> Optional[dict]:
        row = self.store.one("SELECT data FROM reports WHERE day=?", (day,))
        if not row:
            return None
        data = json.loads(row["data"])
        if item_id not in {i["id"] for s in data["sections"] for i in s["items"] if i["point"]}:
            raise ValueError("report.err.item")
        if done:
            self.store.execute("INSERT OR REPLACE INTO report_marks(day, item, done) VALUES(?,?,?)", (day, item_id, self.clock()))
        else:
            self.store.execute("DELETE FROM report_marks WHERE day=? AND item=?", (day, item_id))
        return self.get(day)

    async def preview(self) -> dict:
        """The report as it would look now (not saved, not delivered)."""
        now = self.clock()
        prev = self.latest()
        since = prev["until"] if prev else max(now - 86400, min(self.epoch(), now - 3600))
        rep = await self.build(since, now, prev, datetime.fromtimestamp(now, self.tz()).date().isoformat(), preview=True)
        rep.pop("controller_snapshot", None)
        rep.pop("family_states", None)
        for s in rep["sections"]:
            for i in s["items"]:
                if i["point"]:
                    i["done"] = None
        rep.update({"read": True, "open": rep["points"], "delivery": {}, "older": None, "newer": None})
        return rep

    # ------------------------------------------------------------------ delivery
    def enabled_channels(self) -> list:
        s = self.settings()
        return [ch for ch in CHANNELS if s["channels"][ch].get("enabled")]

    async def deliver(self, report: dict, only: Optional[list] = None) -> dict:
        chans = only if only is not None else self.enabled_channels()
        if not chans:
            return {}
        from . import deliver as dl
        from .render import short_text
        s = self.settings()
        lang = self.store.get_setting("language", None) or "en"
        plain = None
        try:
            if self.plain_delivery():
                plain = (report.get("plain") or {}).get("text") or None
        except Exception:
            plain = None
        msg = short_text(report, lang, self.tz(), s.get("link") or "", plain=plain)
        row = self.store.one("SELECT delivery FROM reports WHERE day=?", (report["day"],))
        state = json.loads(row["delivery"] or "{}") if row else {}
        for ch in chans:
            ok, key = await asyncio.to_thread(dl.send, ch, s["channels"][ch], self._secrets(ch), msg)
            prev = state.get(ch) or {}
            state[ch] = {"ok": ok, "key": key, "t": self.clock(), "tries": (prev.get("tries") or 0) + 1}
            if not ok:
                log.info("nightly report: delivery via %s failed (%s), will retry", ch, key)
        if row:
            self.store.execute("UPDATE reports SET delivery=? WHERE day=?", (json.dumps(state), report["day"]))
        return state

    async def retry_deliveries(self, now: float) -> None:
        row = self.store.one("SELECT day, data, delivery FROM reports ORDER BY day DESC LIMIT 1")
        if not row:
            return
        state = json.loads(row["delivery"] or "{}")
        enabled = set(self.enabled_channels())
        redo = [ch for ch, v in state.items() if ch in enabled and not v.get("ok") and (v.get("tries") or 0) < DELIVERY_RETRIES
                and now - (v.get("t") or 0) >= DELIVERY_RETRY_S]
        if redo:
            await self.deliver(json.loads(row["data"]), redo)

    async def test_channel(self, ch: str) -> dict:
        if ch not in CHANNELS:
            raise ValueError("report.err.channel")
        from . import deliver as dl
        from .render import test_text
        s = self.settings()
        cfg = dict(s["channels"][ch], enabled=True)
        if dl.missing(ch, cfg, self._secrets(ch)):
            return {"ok": False, "key": "report.err.channel_incomplete"}
        lang = self.store.get_setting("language", None) or "en"
        ok, key = await asyncio.to_thread(dl.send, ch, cfg, self._secrets(ch), test_text(lang, s.get("link") or ""))
        return {"ok": ok, "key": key}
