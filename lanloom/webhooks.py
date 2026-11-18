"""Webhooks: Lanloom tells another program about events by calling an address an administrator entered.

Nothing is sent before an administrator adds a webhook. Each webhook has a name, an address (http or https; https is
recommended), the events it wants and a secret that only Lanloom and the receiver know.

Delivery (one HTTP POST per event and webhook, JSON body)::

    POST <address>
    Content-Type: application/json
    User-Agent: lanloom/<version>
    X-Lanloom-Event: device.new
    X-Lanloom-Delivery: dlv_3f9c...          same value on every retry: the receiver can drop duplicates
    X-Lanloom-Timestamp: 1795032261          seconds since 1970 (UTC), new on every attempt
    X-Lanloom-Signature: v1=<hex>            HMAC-SHA256(secret, "<timestamp>.<body>")

    {"id": "dlv_3f9c...", "event": "device.new", "created": 1795032260.1, "lanloom": "1.44.0", "data": {...}}

The receiver recomputes the signature over the timestamp, a dot and the raw body, compares it in constant time and
refuses a timestamp older than five minutes - so a recorded delivery cannot be replayed later.

A delivery counts as done on any 2xx answer. Anything else (another status, no answer within 10 s, a redirect - those
are never followed) is retried with a growing pause: 30 s, 2 min, 10 min, 30 min, 2 h, 6 h; after that it is marked
failed. 410 Gone stops at once. Every attempt is written to the delivery log (status code, error kind, duration -
never the answer's body). The log keeps 30 days.

The address and the secret are kept encrypted (vault) and never shown again after saving; the list only shows the
host. Events are fixed at the moment they happen: the body that is retried is the body that was made then.
"""
import hashlib
import hmac
import json
import logging
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from typing import Callable, Optional

from . import __version__

log = logging.getLogger("lanloom")

SIG_VERSION = "v1"
TOLERANCE_S = 300                      # what the docs tell receivers: refuse timestamps older than this
BACKOFF_S = (30, 120, 600, 1800, 7200, 21600)
MAX_ATTEMPTS = len(BACKOFF_S) + 1
TIMEOUT_S = 10
MAX_HOOKS = 20
MAX_PENDING = 500                      # per webhook; older waiting deliveries are dropped beyond this
KEEP_DAYS = 30
KEEP_ROWS = 5000
NAME_MAX = 60
URL_MAX = 500
LOOP_S = 2.0
DUE_BATCH = 20
FRESH_S = 600                          # an event about the past (sample data, catch-up) is no news

Event = namedtuple("Event", "id group order available")


def _incidents(core) -> bool:
    return bool(getattr(core, "incidents", None))


def _backups(core) -> bool:
    return getattr(core, "backups", None) is not None


EVENTS = {e.id: e for e in (
    Event("device.new", "devices", 10, None),
    Event("device.gone", "devices", 11, None),
    Event("device.offline", "devices", 12, None),
    Event("device.online", "devices", 13, None),
    Event("wan.down", "internet", 20, None),
    Event("wan.up", "internet", 21, None),
    Event("incident.opened", "incidents", 30, _incidents),
    Event("incident.closed", "incidents", 31, _incidents),
    Event("report.ready", "lanloom", 40, None),
    Event("backup.stale", "lanloom", 41, _backups),
)}
PING = "ping"                          # the test delivery; not something to subscribe to
JOURNAL_MAP = {"dev_new": "device.new", "dev_gone": "device.gone", "dev_offline": "device.offline", "dev_back": "device.online",
               "wan_down": "wan.down", "wan_up": "wan.up"}


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), str(int(timestamp)).encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return "%s=%s" % (SIG_VERSION, mac)


def verify(secret: str, timestamp, body: bytes, signature: str, now: Optional[float] = None, tolerance: int = TOLERANCE_S) -> bool:
    """Receiver side (also used by the tests and shown in docs/API.md): signature and age of one delivery."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    if abs(now - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, ts, body), str(signature or ""))


def check_url(url: str) -> str:
    url = str(url or "").strip()
    if not url or len(url) > URL_MAX or any(c.isspace() for c in url):
        raise ValueError("webhooks.err.url")
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("webhooks.err.url")
    if p.username or p.password:
        raise ValueError("webhooks.err.url_userinfo")
    return url


def mask_url(url: str) -> str:
    """What the list shows of an address: scheme and host (the path may itself be a secret)."""
    try:
        p = urllib.parse.urlsplit(url or "")
        host = p.hostname or ""
        if ":" in host:
            host = "[%s]" % host
        port = ":%d" % p.port if p.port else ""
        return "%s://%s%s/…" % (p.scheme, host, port) if host else ""
    except ValueError:
        return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect not followed", headers, fp)


class Webhooks:
    def __init__(self, core, store, vault, clock: Callable[[], float] = time.time):
        self.core = core
        self.store = store
        self.vault = vault
        self.clock = clock
        self._lock = threading.RLock()
        self.post: Optional[Callable] = None     # (url, body, headers) -> status code; replaced in tests
        self._last_prune = 0.0

    # ------------------------------------------------------------------ configuration
    def _hooks(self) -> list:
        raw = self.store.get_setting("webhooks", []) or []
        return [h for h in raw if isinstance(h, dict) and h.get("id")]

    def _save(self, hooks: list) -> None:
        self.store.set_setting("webhooks", hooks)

    def events(self) -> list:
        return [{"id": e.id, "group": e.group, "available": bool(e.available is None or e.available(self.core))}
                for e in sorted(EVENTS.values(), key=lambda e: e.order)]

    def hook(self, hid: str) -> Optional[dict]:
        return next((h for h in self._hooks() if h["id"] == hid), None)

    def view(self, h: dict) -> dict:
        url = self.vault.get("webhook:%s:url" % h["id"]) if self.vault else None
        st = self.store.one("SELECT COUNT(*) AS n FROM webhook_deliveries WHERE hook=? AND status='pending'", (h["id"],))
        return {"id": h["id"], "name": h.get("name") or "", "events": list(h.get("events") or []), "enabled": bool(h.get("enabled")),
                "target": mask_url(url or "") or h.get("target") or "", "https": (url or h.get("target") or "").startswith("https://"),
                "created": h.get("created"), "by": h.get("by") or "", "last_ok": h.get("last_ok"), "last_t": h.get("last_t"),
                "last_key": h.get("last_key") or "", "last_code": h.get("last_code"), "fails": int(h.get("fails") or 0),
                "pending": st["n"] if st else 0, "sample": bool(h.get("sample"))}

    def list(self) -> list:
        return [self.view(h) for h in self._hooks()]

    def save(self, data: dict, by: str = "") -> dict:
        """Add (no ``id``) or change a webhook. A new one gets a secret, returned once as ``secret``. Raises ValueError."""
        name = " ".join(str(data.get("name") or "").split())[:NAME_MAX]
        if not name:
            raise ValueError("webhooks.err.name")
        events = data.get("events")
        if not isinstance(events, list) or not events or any(e not in EVENTS for e in events):
            raise ValueError("webhooks.err.events")
        events = [e for e in sorted(EVENTS, key=lambda k: EVENTS[k].order) if e in events]
        url = data.get("url")
        with self._lock:
            hooks = self._hooks()
            hid = data.get("id")
            out_secret = None
            if hid:
                h = next((x for x in hooks if x["id"] == hid), None)
                if h is None:
                    raise LookupError("webhooks.err.missing")
                if url:
                    self.vault.put("webhook:%s:url" % hid, check_url(url))
                    h.pop("target", None)
                    h["fails"] = 0
                elif not self.vault.has("webhook:%s:url" % hid):
                    raise ValueError("webhooks.err.url")
                if not self.vault.has("webhook:%s:secret" % hid):
                    out_secret = "whsec_" + secrets.token_urlsafe(32)
                    self.vault.put("webhook:%s:secret" % hid, out_secret)
            else:
                if len(hooks) >= MAX_HOOKS:
                    raise ValueError("webhooks.err.too_many")
                url = check_url(url)
                hid = "wh-" + secrets.token_hex(5)
                h = {"id": hid, "created": self.clock(), "by": str(by or "")[:64]}
                hooks.append(h)
                self.vault.put("webhook:%s:url" % hid, url)
                out_secret = "whsec_" + secrets.token_urlsafe(32)
                self.vault.put("webhook:%s:secret" % hid, out_secret)
            h.update(name=name, events=events, enabled=bool(data.get("enabled", True)))
            h.pop("sample", None)
            self._save(hooks)
        out = self.view(h)
        if out_secret:
            out["secret"] = out_secret
        return out

    def rotate(self, hid: str) -> dict:
        with self._lock:
            h = self.hook(hid)
            if h is None:
                raise LookupError("webhooks.err.missing")
            secret = "whsec_" + secrets.token_urlsafe(32)
            self.vault.put("webhook:%s:secret" % hid, secret)
        out = self.view(h)
        out["secret"] = secret
        return out

    def delete(self, hid: str) -> bool:
        with self._lock:
            hooks = self._hooks()
            keep = [h for h in hooks if h["id"] != hid]
            if len(keep) == len(hooks):
                return False
            self._save(keep)
            for part in ("url", "secret"):
                self.vault.delete("webhook:%s:%s" % (hid, part))
            self.store.execute("DELETE FROM webhook_deliveries WHERE hook=?", (hid,))
        return True

    def deliveries(self, hid: Optional[str] = None, limit: int = 50) -> list:
        limit = max(1, min(int(limit or 50), 200))
        rows = self.store.query("SELECT id, hook, event, created, status, attempts, next_at, last_t, last_code, last_key, ms, body"
                                " FROM webhook_deliveries %s ORDER BY id DESC LIMIT ?" % ("WHERE hook=?" if hid else ""),
                                ((hid,) if hid else ()) + (limit,))
        for r in rows:
            try:
                r["delivery"] = json.loads(r.pop("body")).get("id")
            except (ValueError, AttributeError):
                r["delivery"] = None
        return rows

    # ------------------------------------------------------------------ events in
    def on_journal(self, kind: str, data: dict, t: float) -> None:
        ev = JOURNAL_MAP.get(kind)
        if not ev or t < self.clock() - FRESH_S:
            return
        if kind == "dev_new" and not self.store.get_setting("setup_done", False):
            return                                   # the first scan finds everything: that is no news
        if ev.startswith("device."):
            payload = {"device": {k: data.get(k) for k in ("id", "title", "ip", "kind", "vendor") if data.get(k) not in (None, "")}}
            if kind == "dev_gone" and data.get("since"):
                payload["offline_since"] = data["since"]
        else:
            payload = {"uplink": data.get("name") or ""}
        self.emit(ev, payload, t)

    def on_notify(self, eid: str, key: str, data: dict, resolve: bool, t: float) -> None:
        """Tap of the notification rules: events that have no journal row (backups, incidents)."""
        if t < self.clock() - FRESH_S:
            return
        if eid == "backup_old" and not resolve:
            self.emit("backup.stale", {"newest_age_s": data.get("age_dur") or None, "none": bool(data.get("none"))}, t)
        elif eid == "incident":
            opened = self.store.get_setting("webhooks_open", {}) or {}
            if resolve:
                if opened.pop(key, None) is None:
                    return
                self.store.set_setting("webhooks_open", opened)
                self.emit("incident.closed", {"incident": key, "title": data.get("title") or ""}, t)
            else:
                opened[key] = t
                self.store.set_setting("webhooks_open", dict(list(opened.items())[-500:]))
                self.emit("incident.opened", {"incident": key, "title": data.get("title") or "",
                                              "severity": data.get("severity") or ""}, t)

    def on_report(self, report: dict) -> None:
        self.emit("report.ready", {"day": report.get("day"), "level": report.get("level"), "points": report.get("points"),
                                   "link": "#/reports/%s" % report.get("day")}, report.get("created"))

    def emit(self, event: str, data: dict, t: Optional[float] = None) -> int:
        """Queue one delivery per enabled webhook that wants ``event``. Returns how many were queued."""
        if event not in EVENTS:
            return 0
        t = self.clock() if t is None else float(t)
        n = 0
        with self._lock:
            for h in self._hooks():
                if not h.get("enabled") or event not in (h.get("events") or []) or h.get("sample"):
                    continue
                self._queue(h["id"], event, data, t)
                n += 1
        return n

    def _queue(self, hid: str, event: str, data: dict, t: float) -> int:
        body = json.dumps({"id": "dlv_" + secrets.token_hex(10), "event": event, "created": round(t, 3), "lanloom": __version__,
                           "data": data}, separators=(",", ":"), ensure_ascii=False, default=str)
        cur = self.store.execute("INSERT INTO webhook_deliveries(hook, event, created, body, status, attempts, next_at) VALUES(?,?,?,?,?,?,?)",
                                 (hid, event, t, body, "pending", 0, 0))
        over = self.store.query("SELECT id FROM webhook_deliveries WHERE hook=? AND status='pending' ORDER BY id DESC LIMIT -1 OFFSET ?",
                                (hid, MAX_PENDING))
        if over:
            self.store.execute("UPDATE webhook_deliveries SET status='dropped', last_key='webhooks.err.backlog' WHERE id IN (%s)"
                               % ",".join(str(int(r["id"])) for r in over))
        return cur.lastrowid

    # ------------------------------------------------------------------ delivering
    def _post(self, url: str, body: bytes, headers: dict) -> int:
        if self.post is not None:
            return self.post(url, body, headers)
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        handlers = [_NoRedirect()]
        if url.startswith("https://"):
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            resp.read(1024)
            return resp.status

    def attempt(self, row: dict) -> dict:
        """Send one delivery once (blocking). Never raises. Returns {ok, code, key, ms}."""
        hid = row["hook"]
        url = self.vault.get("webhook:%s:url" % hid) if self.vault else None
        secret = self.vault.get("webhook:%s:secret" % hid) if self.vault else None
        if not url or not secret:
            return {"ok": False, "code": None, "key": "webhooks.err.missing", "ms": 0, "final": True}
        body = row["body"].encode("utf-8")
        ts = int(self.clock())
        try:
            delivery = json.loads(row["body"]).get("id") or ""
        except ValueError:
            delivery = ""
        headers = {"Content-Type": "application/json", "User-Agent": "lanloom/%s" % __version__, "X-Lanloom-Event": row["event"],
                   "X-Lanloom-Delivery": delivery, "X-Lanloom-Timestamp": str(ts), "X-Lanloom-Signature": sign(secret, ts, body)}
        t0 = time.monotonic()
        code, final = None, False
        try:
            code = int(self._post(url, body, headers))
            ok = 200 <= code < 300
            key = "webhooks.send.ok" if ok else "webhooks.send.status"
        except urllib.error.HTTPError as exc:
            code, ok = exc.code, False
            key = "webhooks.send.redirect" if 300 <= exc.code < 400 else "webhooks.send.status"
            final = exc.code == 410
        except (ssl.SSLError, ssl.CertificateError):
            ok, key = False, "webhooks.send.tls"
        except (socket.timeout, TimeoutError):
            ok, key = False, "webhooks.send.timeout"
        except (urllib.error.URLError, OSError):
            ok, key = False, "webhooks.send.unreachable"
        except Exception:
            ok, key = False, "webhooks.send.error"
        return {"ok": ok, "code": code, "key": key, "ms": int((time.monotonic() - t0) * 1000), "final": final}

    def _record(self, row: dict, res: dict, now: float) -> None:
        attempts = int(row["attempts"] or 0) + 1
        if res["ok"]:
            status, next_at = "ok", 0
        elif res.get("final") or attempts >= MAX_ATTEMPTS:
            status, next_at = "failed", 0
        else:
            status, next_at = "pending", now + BACKOFF_S[attempts - 1]
        self.store.execute("UPDATE webhook_deliveries SET status=?, attempts=?, next_at=?, last_t=?, last_code=?, last_key=?, ms=? WHERE id=?",
                           (status, attempts, next_at, now, res["code"], res["key"], res["ms"], row["id"]))
        with self._lock:
            hooks = self._hooks()
            h = next((x for x in hooks if x["id"] == row["hook"]), None)
            if h is not None:
                h.update(last_t=now, last_key=res["key"], last_code=res["code"])
                if res["ok"]:
                    h["last_ok"], h["fails"] = now, 0
                else:
                    h["fails"] = int(h.get("fails") or 0) + 1
                self._save(hooks)

    def deliver_due(self, now: Optional[float] = None) -> int:
        """Send what is due (blocking; called in a worker thread by the loop). Returns the number of attempts."""
        now = self.clock() if now is None else now
        rows = self.store.query("SELECT * FROM webhook_deliveries WHERE status='pending' AND next_at <= ? ORDER BY next_at, id LIMIT ?",
                                (now, DUE_BATCH))
        enabled = {h["id"] for h in self._hooks() if h.get("enabled")}
        n = 0
        for row in rows:
            if row["hook"] not in enabled:
                continue                                 # switched off: waits until switched on again (or is pruned)
            self._record(row, self.attempt(row), self.clock())
            n += 1
        return n

    def test(self, hid: str, by: str = "") -> dict:
        """Send a ``ping`` delivery now (blocking) and log it. Raises LookupError."""
        h = self.hook(hid)
        if h is None:
            raise LookupError("webhooks.err.missing")
        now = self.clock()
        body = json.dumps({"id": "dlv_" + secrets.token_hex(10), "event": PING, "created": round(now, 3), "lanloom": __version__,
                           "data": {"webhook": h.get("name") or "", "by": str(by or "")[:64]}}, separators=(",", ":"), ensure_ascii=False)
        cur = self.store.execute("INSERT INTO webhook_deliveries(hook, event, created, body, status, attempts, next_at) VALUES(?,?,?,?,?,?,?)",
                                 (hid, PING, now, body, "pending", 0, 0))
        row = self.store.one("SELECT * FROM webhook_deliveries WHERE id=?", (cur.lastrowid,))
        res = self.attempt(row)
        res["final"] = True                              # a test is never retried
        self._record(row, res, self.clock())
        return {k: res[k] for k in ("ok", "code", "key", "ms")}

    def redeliver(self, delivery_id: int) -> bool:
        """Queue a failed or dropped delivery once more (same body, same delivery id)."""
        return self.store.execute("UPDATE webhook_deliveries SET status='pending', attempts=0, next_at=0 WHERE id=? AND status IN ('failed','dropped')",
                                  (int(delivery_id),)).rowcount > 0

    # ------------------------------------------------------------------ for the nightly report
    def summary(self, since: float, until: float) -> dict:
        rows = self.store.query("SELECT hook, status, event FROM webhook_deliveries WHERE created > ? AND created <= ? AND event != ?",
                                (since, until, PING))
        names = {h["id"]: h.get("name") or h["id"] for h in self._hooks()}
        failed = sorted({names.get(r["hook"], r["hook"]) for r in rows if r["status"] in ("failed", "dropped")})
        return {"hooks": len(names), "sent": sum(1 for r in rows if r["status"] == "ok"),
                "failed": sum(1 for r in rows if r["status"] in ("failed", "dropped")), "failed_hooks": failed,
                "pending": sum(1 for r in rows if r["status"] == "pending"),
                "failing_now": sorted(names[h["id"]] for h in self._hooks() if h.get("enabled") and int(h.get("fails") or 0) >= 3)}

    def prune(self) -> None:
        now = self.clock()
        self.store.execute("DELETE FROM webhook_deliveries WHERE created < ? AND status != 'pending'", (now - KEEP_DAYS * 86400,))
        self.store.execute("DELETE FROM webhook_deliveries WHERE id NOT IN (SELECT id FROM webhook_deliveries ORDER BY id DESC LIMIT ?)", (KEEP_ROWS,))
        known = {h["id"] for h in self._hooks()}
        if self.vault:
            for r in self.store.query("SELECT name FROM secrets WHERE name LIKE 'webhook:%'"):
                parts = r["name"].split(":")
                if len(parts) == 3 and parts[1] not in known:
                    self.vault.delete(r["name"])

    # ------------------------------------------------------------------ loop
    async def run(self) -> None:
        import asyncio
        while True:
            try:
                if not getattr(self.core, "demo", None):
                    if self.store.one("SELECT 1 AS x FROM webhook_deliveries WHERE status='pending' AND next_at <= ? LIMIT 1", (self.clock(),)):
                        await asyncio.to_thread(self.deliver_due)
                if self.clock() - self._last_prune > 6 * 3600:
                    self._last_prune = self.clock()
                    self.prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("webhooks: round failed")
                await asyncio.sleep(60)
            await asyncio.sleep(LOOP_S)


def seed_demo(webhooks: "Webhooks", now: Optional[float] = None) -> None:
    """Sample webhooks with a few log lines (demo mode: nothing is ever sent, the addresses are reserved test names)."""
    if webhooks.store.get_setting("webhooks_demo_seeded"):
        return
    now = time.time() if now is None else now
    hooks = [
        {"id": "wh-sample1", "name": "Home automation", "events": ["device.new", "device.offline", "device.online", "wan.down", "wan.up"],
         "enabled": True, "created": now - 12 * 86400, "by": "admin", "target": "https://hub.example.invalid/…", "sample": True,
         "last_ok": now - 3600, "last_t": now - 3600, "last_key": "webhooks.send.ok", "last_code": 204, "fails": 0},
        {"id": "wh-sample2", "name": "Chat channel", "events": ["report.ready", "backup.stale"], "enabled": True,
         "created": now - 4 * 86400, "by": "admin", "target": "https://chat.example.invalid/…", "sample": True,
         "last_ok": now - 2 * 86400, "last_t": now - 7 * 3600, "last_key": "webhooks.send.timeout", "last_code": None, "fails": 3},
    ]
    webhooks.store.set_setting("webhooks", hooks)
    log_rows = [("wh-sample1", "device.new", 5 * 3600, "ok", 1, 204, "webhooks.send.ok", 41),
                ("wh-sample1", "device.offline", 3 * 3600, "ok", 1, 204, "webhooks.send.ok", 38),
                ("wh-sample1", "device.online", 3600, "ok", 2, 204, "webhooks.send.ok", 44),
                ("wh-sample2", "report.ready", 31 * 3600, "ok", 1, 200, "webhooks.send.ok", 230),
                ("wh-sample2", "report.ready", 7 * 3600, "failed", MAX_ATTEMPTS, None, "webhooks.send.timeout", 10000)]
    for hid, ev, ago, status, attempts, code, key, ms in log_rows:
        body = json.dumps({"id": "dlv_" + secrets.token_hex(10), "event": ev, "created": now - ago, "lanloom": __version__, "data": {}})
        webhooks.store.execute("INSERT INTO webhook_deliveries(hook, event, created, body, status, attempts, next_at, last_t, last_code, last_key, ms)"
                               " VALUES(?,?,?,?,?,?,?,?,?,?,?)", (hid, ev, now - ago, body, status, attempts, 0, now - ago + 1, code, key, ms))
    webhooks.store.set_setting("webhooks_demo_seeded", True)
