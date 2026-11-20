"""Several sites in one Lanloom: a home, a holiday flat, the office of a club - each with its own networks, sources,
devices, history and nightly report.

Model
-----
A site is the level above networks, sources, devices and reports. Every site has a core of its own (``core.Core``)
with a database of its own; nothing of one site is ever stored in the tables of another. The site this Lanloom runs
in is ``home`` and keeps the database it always had (``lanloom.db`` - an update adds a row to the site list and
nothing moves, so an existing installation simply becomes the site "Home" without losing anything). Further sites
keep their data in ``<data>/sites/<id>/lanloom.db``.

Accounts, sessions, the audit log, backups, API keys, webhooks and the updater belong to the instance, not to a site.
The firewall editor, the executor and the AI assistant work on the home site only (``PRIMARY`` routes).

Reaching another site
---------------------
Lanloom never scans across a router on its own. A remote site is seen only over a way that is already there:

* a controller that manages several sites (the integration of the remote site names the controller's site), or
* the host agent (E4) on a machine at the remote site, paired with a code made in that site.

The local interfaces, the presence check and NetFlow belong to the home site; a remote site does not use them. A
scan of a remote range needs the user's explicit consent for that range (as for routed networks at home).

Visibility
----------
Administrators see every site. Every other account sees the sites an administrator released for it (``users.sites``:
``*`` = all sites, also the ones added later; otherwise a list of site ids). The server picks the site of a request
(cookie ``ll_site`` or header ``X-Lanloom-Site``) only among the released ones and answers every site-bound route from
that site's core, so names and addresses of another site never reach the answer. A browser remembers its last site.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("lanloom.sites")

HOME = "home"
COOKIE = "ll_site"
HEADER = "x-lanloom-site"
MIRROR = "X-Lanloom-Site"        # the answer names the site it was made for (M2)
WS_CHANGED = 4409               # live socket: the page asked for a site that is not released (any more)
ACTIVE = contextvars.ContextVar("lanloom_site", default=None)
# the sites whose core a request actually read (M1T): the answer names the site of the data it delivers, not the site the
# request asked for; a request that reads two cores is a fault and is never answered with one site's name
SERVED = contextvars.ContextVar("lanloom_served", default=None)
MIXED = "mixed"                  # the answer read more than one site: the page throws it away
MAX_SITES = 16
NAME_MAX = 60
ID_RE = re.compile(r"^(home|s-[0-9a-f]{8})$")
KINDS = ("local", "remote")
REPORT_MODES = ("per_site", "combined")
ALL = "*"
FOLDER = "sites"

# Routes that are not bound to one site. They read or change the instance (own account, users, backups, updates, API
# keys ...) or are answered without a session. Every other /api route is answered from the core of the request's site.
GLOBAL_PREFIXES = (
    "/api/health", "/api/state", "/api/i18n/", "/api/setup/account", "/api/login", "/api/logout", "/api/passkey",
    "/api/invite/", "/api/account", "/api/users", "/api/security", "/api/audit", "/api/diagnostics", "/api/backup",
    "/api/setup/restore", "/api/update", "/api/whatsnew", "/api/tokens", "/api/webhooks", "/api/mqtt-out",
    "/api/sites", "/api/agent/", "/api/executor/", "/api/ai/installer/", "/api/ai/pair",
)
# Routes that work on the home site only (the firewall editor with its drafts and the executor, the AI assistant).
# Another site gets 409 site.err.primary_only; the web page hides these parts there.
PRIMARY_PREFIXES = ("/api/firewall", "/api/ai", "/api/executor/")


def note_served(sid) -> None:
    """A request read the core of site ``sid`` (called where the app hands out a core)."""
    seen = SERVED.get()
    if seen is not None and sid:
        seen.add(sid)


def served_name(picked: Optional[str], scope: str) -> Optional[str]:
    """The site an answer names in its header: the site of the core whose data it carries (one site), ``MIXED`` when it
    read more than one, the picked site when it read none (an error before the handler, a route of the instance)."""
    seen = SERVED.get() or set()
    if scope != "global" and seen:
        return next(iter(seen)) if len(seen) == 1 else MIXED
    return picked


def scope_of(path: str) -> str:
    """"global" | "primary" | "site" for an /api route template (or a concrete path)."""
    if path.startswith(PRIMARY_PREFIXES) and not path.startswith(("/api/ai/installer/", "/api/ai/pair")):
        return "primary"
    if path.startswith(GLOBAL_PREFIXES) or path == "/api/agent" or not path.startswith("/api/"):
        return "global"
    return "site"


def clean_name(name) -> str:
    text = " ".join(str(name or "").split())
    return "".join(c for c in text if ord(c) >= 32)[:NAME_MAX]


def parse_user_sites(raw) -> object:
    """The stored value of ``users.sites``: ``"*"`` or a list of site ids. Anything that is not exactly ``"*"`` or a list
    of site ids releases nothing - an empty or broken value never means "all sites" (M1T)."""
    if raw == ALL:
        return ALL
    if raw in (None, ""):
        return []
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    if val == ALL:
        return ALL
    if not isinstance(val, list):
        return []
    return [s for s in val if isinstance(s, str) and ID_RE.match(s)][:MAX_SITES]


def is_admin(user: Optional[dict]) -> bool:
    if not user:
        return False
    return user.get("role") == "admin" or user.get("account_role") == "admin"


def _sees_clients(kind: str) -> bool:
    """A source that lists the devices of a network: the generic controller or a family that reads clients."""
    if kind == "controller":
        return True
    if kind in ("hub", "mqtt", "snmp", "host"):
        return False
    try:
        from . import integrations
        read = integrations.get(kind).capabilities.get("read") or []
    except Exception:
        return False
    return "clients" in read or "inventory" in read


class Sites:
    """The site list of this instance and the running core of every site."""

    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.cores: dict = {HOME: app.real_core}
        self._lock = None
        self.version = 0                 # rises when the list changes (the web page reloads the switcher)
        self._ensure_home()

    # ------------------------------------------------------------------ list
    def _ensure_home(self) -> None:
        if not self.store.one("SELECT id FROM sites WHERE id=?", (HOME,)):
            # an existing installation becomes the site "Home": its database stays where it is, nothing moves
            self.store.execute("INSERT INTO sites(id, name, kind, created, pos) VALUES(?,?,?,?,?)",
                               (HOME, "", "local", time.time(), 0))

    def rows(self) -> list:
        return self.store.query("SELECT id, name, kind, created, pos FROM sites ORDER BY pos, created, id")

    def ids(self) -> list:
        return [r["id"] for r in self.rows()]

    def get(self, sid) -> Optional[dict]:
        if not isinstance(sid, str) or not ID_RE.match(sid):
            return None
        return self.store.one("SELECT id, name, kind, created, pos FROM sites WHERE id=?", (sid,))

    def several(self) -> bool:
        return len(self.cores) > 1

    def core(self, sid) -> Optional[object]:
        return self.cores.get(sid)

    def name(self, sid) -> str:
        row = self.get(sid)
        return (row or {}).get("name") or ""

    # ------------------------------------------------------------------ cores
    def _store_path(self, sid: str) -> str:
        if self.store.path == ":memory:":
            return ":memory:"
        return str(Path(self.app.cfg.data_dir) / FOLDER / sid / "lanloom.db")

    def _build(self, row: dict):
        from .core import Core
        from .crypto import Vault
        from .store import Store
        app = self.app
        home = app.real_core
        store = Store(self._store_path(row["id"]))
        core = Core(app.cfg, store, Vault(store, app.instance_key), site=row["id"],
                    remote=row["kind"] != "local", shared=home)
        self.wire(core)
        if getattr(app.cfg, "demo", False):
            from .demo_sites import BranchDemo
            core.demo = BranchDemo()
        return core

    def wire(self, core) -> None:
        """Connect a site core to the instance: people, channels and instance-wide settings live in the home store."""
        app = self.app
        home = app.real_core
        core.site_hub = self
        if core is home:
            return
        core.people_store = app.store
        core.people_vault = app.vault
        core.instance_store = app.store
        core.channel_reports = home.reports
        core.reports.update_state = app.updates.state
        ingest = getattr(app, "ingest", None)
        if ingest is not None:                 # external reports delivered for this site (M1T), shown in this site's nightly report
            core.api_ingest = ingest.of_site(core.site_id)
        hooks = getattr(app, "webhooks", None)
        if hooks is not None:                  # webhooks are set up once for the instance; a site's events name the site
            sid = core.site_id
            tag = lambda: {"id": sid, "name": self.name(sid)}      # noqa: E731
            core.reports.journal.listeners.append(lambda k, d, t: hooks.on_journal(k, d, t, site=tag()))
            core.notify.taps.append(lambda eid, key, d, resolve, t: hooks.on_notify(eid, key, d, resolve, t, site=tag()))
            core.reports.report_listeners.append(lambda rep: hooks.on_report(rep, site=tag()))

    async def start(self) -> None:
        """Start the core of every further site (the home core is started by the app)."""
        for row in self.rows():
            if row["id"] != HOME and row["id"] not in self.cores:
                await self._start_one(row)

    async def _start_one(self, row: dict):
        core = await asyncio.to_thread(self._build, row)
        self.cores[row["id"]] = core
        try:
            await core.start()
        except Exception:
            log.exception("a site could not be started")
        return core

    async def stop(self) -> None:
        for sid, core in list(self.cores.items()):
            if sid == HOME:
                continue
            try:
                await core.stop()
            except Exception:
                log.exception("a site did not stop cleanly")

    # ------------------------------------------------------------------ changes (administrators)
    async def create(self, name, kind: str = "remote") -> dict:
        name = clean_name(name)
        if not name:
            raise ValueError("site.err.name")
        if kind not in KINDS or kind == "local":
            raise ValueError("site.err.kind")        # exactly one site is where Lanloom runs: home
        if len(self.rows()) >= MAX_SITES:
            raise ValueError("site.err.too_many")
        if any((r["name"] or "").lower() == name.lower() for r in self.rows()):
            raise ValueError("site.err.name_taken")
        sid = "s-" + secrets.token_hex(4)
        pos = max([r["pos"] for r in self.rows()] + [0]) + 1
        self.store.execute("INSERT INTO sites(id, name, kind, created, pos) VALUES(?,?,?,?,?)",
                           (sid, name, kind, time.time(), pos))
        row = self.get(sid)
        await self._start_one(row)
        self.version += 1
        return self.public_row(row)

    def rename(self, sid, name) -> dict:
        row = self.get(sid)
        if not row:
            raise LookupError("site.err.unknown")
        name = clean_name(name)
        if not name and sid != HOME:
            raise ValueError("site.err.name")
        if name and any(r["id"] != sid and (r["name"] or "").lower() == name.lower() for r in self.rows()):
            raise ValueError("site.err.name_taken")
        self.store.execute("UPDATE sites SET name=? WHERE id=?", (name, sid))
        self.version += 1
        return self.public_row(self.get(sid))

    async def delete(self, sid) -> bool:
        if sid == HOME:
            raise ValueError("site.err.home")
        row = self.get(sid)
        if not row:
            return False
        core = self.cores.pop(sid, None)
        if core is not None:
            try:
                await core.stop()
            except Exception:
                log.exception("a site did not stop cleanly before it was removed")
            try:
                core.store.close()
            except Exception:
                pass
        self.store.execute("DELETE FROM sites WHERE id=?", (sid,))
        for u in self.store.query("SELECT id, sites FROM users"):
            cur = parse_user_sites(u["sites"])
            if cur != ALL and sid in cur:
                self.store.execute("UPDATE users SET sites=? WHERE id=?", (json.dumps([s for s in cur if s != sid]), u["id"]))
        if self.store.path != ":memory:":
            folder = Path(self.app.cfg.data_dir) / FOLDER / sid
            if folder.is_dir() and folder.parent.name == FOLDER:
                shutil.rmtree(folder, ignore_errors=True)
        self.version += 1
        return True

    async def reload(self) -> None:
        """After a restore: start the sites of the restored list, stop the ones it no longer has."""
        self._ensure_home()
        want = {r["id"]: r for r in self.rows()}
        for sid in [s for s in self.cores if s != HOME and s not in want]:
            core = self.cores.pop(sid)
            try:
                await core.stop()
            except Exception:
                pass
        for sid, row in want.items():
            if sid == HOME:
                continue
            core = self.cores.get(sid)
            if core is None:
                await self._start_one(row)
            elif hasattr(core, "reload_after_restore"):
                core.reload_after_restore()
        self.version += 1

    # ------------------------------------------------------------------ visibility
    def user_sites(self, user: Optional[dict]):
        if not user:
            return []
        if is_admin(user):
            return ALL
        uid = user.get("id")
        if uid is None:
            return []
        row = self.store.one("SELECT sites FROM users WHERE id=?", (uid,))
        return parse_user_sites(row["sites"]) if row else []

    def allowed(self, user: Optional[dict]) -> list:
        """Site ids this person may see, in list order (only running sites)."""
        mine = self.user_sites(user)
        order = [sid for sid in self.ids() if sid in self.cores]
        if mine == ALL:
            return order
        return [sid for sid in order if sid in mine]

    def may_see(self, user: Optional[dict], sid: str) -> bool:
        return sid in self.allowed(user)

    def pick(self, user: Optional[dict], requested) -> Optional[str]:
        """The site of a request that names no site: the requested one when released for this person, else the first
        released one. A request that NAMES a site goes through ``resolve`` and never falls back (M1T)."""
        allowed = self.allowed(user)
        if isinstance(requested, str) and requested in allowed:
            return requested
        return allowed[0] if allowed else None

    def resolve(self, user: Optional[dict], how: Optional[str], asked) -> tuple:
        """(site id, unknown) of a request. ``how`` says where the request named its site (header, address, cookie) or is
        None when it named none. A named site that is empty, unknown or not released for this person is ``unknown``:
        the request is refused (404), never answered from another site - not even the person's first one (M1T). Only a
        request that names no site at all gets the person's first released site."""
        allowed = self.allowed(user)
        if how is None:
            return (allowed[0] if allowed else None), False
        if isinstance(asked, str) and asked in allowed:
            return asked, False
        return None, True

    def recipients(self, sid: str, users: list) -> list:
        """The people (rows with id and role) who may see site ``sid`` - notifications of a site go only to them."""
        out = []
        for u in users:
            if self.may_see({"id": u.get("id"), "role": u.get("role")}, sid):
                out.append(u)
        return out

    def set_user_sites(self, user_id: int, value) -> object:
        if value == ALL:
            clean = ALL
        elif isinstance(value, list) and all(isinstance(s, str) for s in value):
            known = set(self.ids())
            if any(s not in known for s in value):
                raise ValueError("site.err.unknown")
            clean = [s for s in self.ids() if s in value]
        else:
            raise ValueError("site.err.sites")
        self.store.execute("UPDATE users SET sites=? WHERE id=?", (ALL if clean == ALL else json.dumps(clean), int(user_id)))
        return clean

    # ------------------------------------------------------------------ answers
    def public_row(self, row: dict) -> dict:
        core = self.cores.get(row["id"])
        return {"id": row["id"], "name": row["name"] or "", "kind": row["kind"], "home": row["id"] == HOME,
                "reach": self.reach(core) if core is not None else []}

    @staticmethod
    def reach(core) -> list:
        """How a site is seen: this host's own interfaces, a controller, a host agent - never more than is set up."""
        out = []
        if not getattr(core, "remote", False):
            out.append("host")
        try:
            kinds = {i["kind"] for i in core.integrations() if i.get("enabled")}
        except Exception:
            kinds = set()
        if any(_sees_clients(k) for k in kinds):
            out.append("controller")
        if "snmp" in kinds:
            out.append("snmp")
        if "hub" in kinds:
            out.append("hub")
        try:
            if core.agents.entries():
                out.append("agent")
        except Exception:
            pass
        return out

    def listing(self, user: Optional[dict], current: Optional[str]) -> dict:
        allowed = self.allowed(user)
        rows = [self.public_row(r) for r in self.rows() if r["id"] in allowed]
        return {"sites": rows, "current": current if current in allowed else (allowed[0] if allowed else None),
                "several": len(rows) > 1, "version": self.version,
                "report_mode": self.report_mode()}

    def report_mode(self) -> str:
        mode = self.store.get_setting("sites_report", "per_site")
        return mode if mode in REPORT_MODES else "per_site"

    def set_report_mode(self, mode) -> str:
        if mode not in REPORT_MODES:
            raise ValueError("site.err.report_mode")
        self.store.set_setting("sites_report", mode)
        return mode

    def overview(self, user: Optional[dict], now: Optional[float] = None) -> dict:
        """Every released site at a glance: state, devices, internet, what stands out."""
        now = time.time() if now is None else now
        out = []
        for sid in self.allowed(user):
            core = self.cores.get(sid)
            row = self.get(sid)
            if core is None or row is None:
                continue
            item = self.public_row(row)
            try:
                s = core.overview.summary(now)
            except Exception:
                log.exception("site overview failed")
                s = None
            if s:
                notes = s.get("notes") or []
                item.update({"level": (s.get("health") or {}).get("level", "ok"), "devices": s.get("devices") or {},
                             "wan": {"state": (s.get("wan") or {}).get("state", "unknown"),
                                     "last_outage": (s.get("wan") or {}).get("last_outage")},
                             "sources": {k: (s.get("sources") or {}).get(k) for k in ("total", "fresh", "stale", "waiting")},
                             "notes": len(notes),
                             "incidents": sum(1 for n in notes if n.get("level") in ("warn", "danger")),
                             "top": [{"key": n.get("key"), "level": n.get("level"), "vars": n.get("vars") or {},
                                      "sub": n.get("sub")} for n in notes[:2]],
                             "empty": bool(s.get("empty")), "report": (s.get("report") or {})})
            else:
                item.update({"level": "unknown", "devices": {}, "wan": {"state": "unknown"}, "sources": {}, "notes": 0,
                             "incidents": 0, "top": [], "empty": True, "report": {}})
            try:
                item["agents"] = len(core.agents.entries())
            except Exception:
                item["agents"] = 0
            out.append(item)
        levels = [i["level"] for i in out]
        worst = "danger" if "danger" in levels else ("warn" if "warn" in levels else "ok")
        return {"t": round(now, 3), "sites": out, "level": worst,
                "devices": sum((i.get("devices") or {}).get("total", 0) or 0 for i in out),
                "online": sum((i.get("devices") or {}).get("online", 0) or 0 for i in out)}

