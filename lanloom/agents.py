"""Host agents and container sources: what runs on the other hosts of the network, and who talks to whom.

Two kinds of entries, both read only:

* **agent** - the optional Lanloom agent on another host (``lanloom.agent``) pushes reports with a token of its own.
  An administrator creates a one-time code (valid 30 minutes); the agent exchanges it once for its token
  (``pair``). Lanloom keeps only the token's SHA-256 hash; revoking deletes it and the agent's next report is refused.
* **proxy** - a container runtime API behind a socket proxy on another host (or this one), polled by Lanloom itself
  every 30 seconds with GET requests only (``lanloom.agent.containers``).

What a report brings is filtered twice (on the agent and here, ``lanloom.agent.scrub``): metadata only, never
contents, environment variables or command lines. From it Lanloom keeps, per host: the latest load, memory, disks,
listening services, failed units, timers, containers and overlay VPN peers (memory, plus a copy in the database so a
restart shows the last state), a 5-minute sample of load/memory/disk/failures for 35 days (host health for the
forecast feature), and the conversations between devices: TCP bytes between the agent's host and another known device
become a dashed "talks to" line on the network map and a pair in the data paths; bytes to the internet go into the data
paths of that host. When both ends run an agent, only one side books a conversation, so no byte counts twice.

**Noise filter**: a container that lived less than ``SHORT_S`` (10 minutes) is not listed on its own - one-off jobs
and throw-away containers only raise a counter. A container whose name was seen long-lived before, a restarting one
and an unhealthy one always show.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import threading
import time
from typing import Optional

from . import __version__
from .agent import PROTO, REPORT_S, scrub
from .agent import containers as ct_mod

log = logging.getLogger("lanloom")

IMAGE = "ghcr.io/robeertm/lanloom"
CODE_VALID_S = 1800
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_INVITES = 10
MAX_AGENTS = 200
MAX_REPORTS = 60                 # reports in one call (an agent catching up after an outage sends batches)
MAX_FLOWS = 600
MAX_BODY = 4 << 20             # bytes of one report call
OFFLINE_S = 90                   # no report for this long: the agent counts as offline
PROXY_EVERY = 30
SHORT_S = 600                    # noise filter: containers that lived less than 10 minutes
SAMPLE_S = 300
KEEP_SAMPLE_DAYS = 35
TALK_HALF_S = 300                # live conversations fade with this half-life
TALK_MIN = 2048
MAX_TALK = 300
SAVE_EVERY = 60
NAME_RX = re.compile(r"[^\w .@()+-]+", re.UNICODE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_hosts (id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'agent', name TEXT NOT NULL,
    token_hash TEXT, url TEXT, created REAL NOT NULL, last_seen REAL, version TEXT NOT NULL DEFAULT '',
    facts TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT '{}', device TEXT);
CREATE TABLE IF NOT EXISTS agent_samples (slot INTEGER NOT NULL, agent TEXT NOT NULL, load REAL, cpus INTEGER, mem REAL,
    disk REAL, failed INTEGER, containers INTEGER, unhealthy INTEGER, PRIMARY KEY (slot, agent));
"""


def token_hash(token: str) -> str:
    return hashlib.sha256(("lanloom-agent-token:" + (token or "")).encode("utf-8")).hexdigest()


def code_hash(code: str) -> str:
    return hashlib.sha256(("lanloom-agent-code:" + normalise_code(code)).encode("utf-8")).hexdigest()


def normalise_code(code) -> str:
    c = re.sub(r"[\s-]", "", str(code or "")).upper()
    return c if re.fullmatch(r"[A-Z0-9]{8}", c) else ""


def clean_name(name, fallback: str = "") -> str:
    s = NAME_RX.sub("", str(name or "")).strip()[:48]
    return scrub.text(s, 48) if s else fallback


def _is_outside(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_global


class AgentHub:
    def __init__(self, core, clock=time.time):
        self.core = core
        self.store = core.store
        self.clock = clock
        self._lock = threading.RLock()
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                self.store.execute(stmt)
        self.live: dict = {}               # agent id -> latest state (see _apply)
        self.talk_acc: dict = {}           # (a, b) a < b -> [bytes a->b, bytes b->a, last t]
        self.version = 0                   # rises when what the map or the Running view shows changed
        self._saved = 0.0
        self._sampled: dict = {}           # agent id -> last sample slot
        self._events: dict = {}            # (agent, key) -> True while a condition is announced
        self._runtimes: dict = {}          # proxy id -> Runtime (keeps the inspect cache)
        self.agents_cache = None
        self._load()

    # ------------------------------------------------------------------ rows
    def _rows(self) -> list:
        return self.store.query("SELECT * FROM agent_hosts ORDER BY created")

    def _load(self) -> None:
        for r in self._rows():
            try:
                st = json.loads(r["state"] or "{}")
            except ValueError:
                st = {}
            st.setdefault("known_names", [])
            st["t"] = r["last_seen"] or 0
            self.live[r["id"]] = st

    def reload(self) -> None:
        """After a restore: forget the in-memory state and read the restored entries."""
        self.live.clear()
        self.talk_acc.clear()
        self._events.clear()
        self._runtimes.clear()
        self.agents_cache = None
        self._load()
        self.version += 1

    def _row(self, aid: str) -> Optional[dict]:
        return self.store.one("SELECT * FROM agent_hosts WHERE id=?", (str(aid)[:40],))

    def entries(self) -> list:
        """All agents and proxies for the settings (never a token or its hash)."""
        now = self.clock()
        out = []
        for r in self._rows():
            st = self.live.get(r["id"]) or {}
            facts = json.loads(r["facts"] or "{}")
            seen = r["last_seen"] or 0
            out.append({"id": r["id"], "kind": r["kind"], "name": r["name"], "created": r["created"], "last_seen": seen or None,
                        "online": bool(seen) and now - seen < (OFFLINE_S if r["kind"] == "agent" else PROXY_EVERY * 3),
                        "version": r["version"] or "", "update": self._update(r), "device": self.device_of(r["id"]),
                        "url": r["url"] if r["kind"] == "proxy" else None, "host": facts.get("name") or "",
                        "ips": (facts.get("ips") or [])[:4], "os": facts.get("os") or "", "error": st.get("error")})
        return out

    def _update(self, row: dict) -> Optional[dict]:
        if row["kind"] != "agent" or not row["version"] or row["version"] == __version__:
            return None
        return {"version": __version__, "image": "%s:%s" % (IMAGE, __version__), "agent": row["version"]}

    # ------------------------------------------------------------------ invitations and pairing
    def invite(self, name: str = "") -> dict:
        now = self.clock()
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        invites = [i for i in (self.store.get_setting("agent_invites", []) or []) if float(i.get("expires") or 0) > now]
        invites = invites[-(MAX_INVITES - 1):] + [{"hash": code_hash(code), "expires": now + CODE_VALID_S, "name": clean_name(name)}]
        self.store.set_setting("agent_invites", invites)
        return {"code": code[:4] + "-" + code[4:], "expires": now + CODE_VALID_S, "image": "%s:%s" % (IMAGE, __version__),
                "version": __version__}

    def has_invite(self, code) -> bool:
        """A valid pairing code of this hub (not used up) - the app looks for the site a code was made in (M1)."""
        c = normalise_code(code)
        if not c:
            return False
        now, h = self.clock(), code_hash(c)
        invites = [i for i in (self.store.get_setting("agent_invites", []) or []) if float(i.get("expires") or 0) > now]
        return any(hmac.compare_digest(str(i.get("hash", "")), h) for i in invites)

    def _take_invite(self, code) -> Optional[dict]:
        c = normalise_code(code)
        if not c:
            return None
        now = self.clock()
        h = code_hash(c)
        with self._lock:
            invites = [i for i in (self.store.get_setting("agent_invites", []) or []) if float(i.get("expires") or 0) > now]
            hit = next((i for i in invites if hmac.compare_digest(str(i.get("hash", "")), h)), None)
            if hit:
                invites = [i for i in invites if i is not hit]       # one-time code: used up
            self.store.set_setting("agent_invites", invites)
        return hit

    def pair(self, code, body: dict, peer_ip: str = "") -> dict:
        if not isinstance(body, dict):
            raise ValueError("agent.err.body")
        if int(body.get("proto") or 0) != PROTO:
            raise ValueError("agent.err.proto")
        inv = self._take_invite(code)
        if inv is None:
            raise PermissionError("agent.err.code")
        if len(self._rows()) >= MAX_AGENTS:
            raise ValueError("agent.err.too_many")
        facts = self._facts(body.get("host"))
        token = secrets.token_urlsafe(32)
        aid = "ag-" + secrets.token_hex(5)
        name = inv.get("name") or facts.get("name") or peer_ip or aid
        now = self.clock()
        self.store.execute("INSERT INTO agent_hosts(id, kind, name, token_hash, created, last_seen, version, facts) VALUES(?,?,?,?,?,?,?,?)",
                           (aid, "agent", clean_name(name, aid), token_hash(token), now, None,
                            scrub.text(body.get("version"), 32) or "", json.dumps(facts, separators=(",", ":"))))
        self.live[aid] = {"known_names": [], "t": 0}
        self._resolve_device(aid, facts)
        self.version += 1
        return {"agent_id": aid, "token": token, "server_version": __version__, "interval": REPORT_S}

    def token_agent(self, authorization: str) -> Optional[str]:
        """Agent id for an ``Authorization: Bearer <token>`` header, None when unknown or revoked."""
        auth = str(authorization or "")
        if not auth.startswith("Bearer ") or len(auth) > 300:
            return None
        h = token_hash(auth[7:].strip())
        row = self.store.one("SELECT id, token_hash FROM agent_hosts WHERE token_hash=? AND kind='agent'", (h,))
        if row and hmac.compare_digest(row["token_hash"], h):
            return row["id"]
        return None

    # ------------------------------------------------------------------ management
    def rename(self, aid: str, name: str) -> bool:
        n = clean_name(name)
        if not n:
            raise ValueError("agent.err.name")
        self.store.execute("UPDATE agent_hosts SET name=? WHERE id=?", (n, str(aid)[:40]))
        ok = self.store.changes() > 0
        self.version += ok
        return ok

    def revoke(self, aid: str) -> bool:
        """Delete the entry with its token hash; its next report is refused. Samples go with it."""
        aid = str(aid)[:40]
        self.store.execute("DELETE FROM agent_hosts WHERE id=?", (aid,))
        ok = self.store.changes() > 0
        if ok:
            self.store.execute("DELETE FROM agent_samples WHERE agent=?", (aid,))
            self.live.pop(aid, None)
            self._runtimes.pop(aid, None)
            for k in [k for k in self._events if k[0] == aid]:
                self._events.pop(k, None)
            self.version += 1
        return ok

    def add_proxy(self, name: str, url: str) -> dict:
        u = ct_mod.normalise_url(url)
        if not u:
            raise ValueError("agent.err.containers_url")
        if len(self._rows()) >= MAX_AGENTS:
            raise ValueError("agent.err.too_many")
        aid = "px-" + secrets.token_hex(5)
        host = u.split("://", 1)[1].split("/")[0].rsplit(":", 1)[0].strip("[]") if not u.startswith("unix:") else ""
        self.store.execute("INSERT INTO agent_hosts(id, kind, name, url, created, facts) VALUES(?,?,?,?,?,?)",
                           (aid, "proxy", clean_name(name, host or aid), u, self.clock(),
                            json.dumps({"ips": [host] if host else []}, separators=(",", ":"))))
        self.live[aid] = {"known_names": [], "t": 0}
        self.version += 1
        return {"id": aid, "url": u}

    @staticmethod
    def test_proxy(url: str) -> dict:
        """One read of a socket proxy for the settings' Test button: runtime, version and number of containers."""
        try:
            rt = ct_mod.Runtime(url)
            info = rt.version()
            n = len(rt.containers(with_bytes=False))
            return dict(info, ok=True, containers=n)
        except ct_mod.SourceError as exc:
            return {"ok": False, "error": exc.key}

    # ------------------------------------------------------------------ reports
    @staticmethod
    def _facts(raw) -> dict:
        raw = raw if isinstance(raw, dict) else {}
        out = scrub.pick(raw, {"name": "text", "kernel": "text", "os": "text", "arch": "text", "cpus": "num", "uptime": "num"})
        ips = []
        for ip in (raw.get("ips") or [])[:16]:
            try:
                ips.append(str(ipaddress.ip_address(str(ip))))
            except ValueError:
                continue
        macs = [str(m).lower() for m in (raw.get("macs") or [])[:16] if re.fullmatch(r"[0-9a-fA-F:]{17}", str(m))]
        out.update(ips=ips, macs=macs)
        return out

    def ingest(self, aid: str, body: dict, now: Optional[float] = None) -> dict:
        """One call of an agent: up to MAX_REPORTS reports, oldest first. Raises ValueError for a malformed call."""
        if not isinstance(body, dict) or not isinstance(body.get("reports"), list):
            raise ValueError("agent.err.body")
        if int(body.get("proto") or 0) != PROTO:
            raise ValueError("agent.err.proto")
        reports = body["reports"]
        if len(reports) > MAX_REPORTS:
            raise ValueError("agent.err.too_many_reports")
        now = self.clock() if now is None else now
        version = scrub.text(body.get("version"), 32) or ""
        latest = 0.0
        for rep in reports:
            if not isinstance(rep, dict):
                continue
            rep = scrub.strip(rep)              # second pass of the secret filter, whatever the agent sent
            t = float(rep.get("t") or now)
            t = min(t, now)                     # a clock ahead of Lanloom's never books into the future
            if now - t > 3 * 3600:
                continue                        # older than the buffer can hold: dropped
            self.ingest_report(aid, rep, t, now)
            latest = max(latest, t)
        self.store.execute("UPDATE agent_hosts SET last_seen=?, version=? WHERE id=?", (now, version, aid))
        return {"ok": True, "server_version": __version__, "interval": REPORT_S}

    def ingest_report(self, aid: str, rep: dict, t: float, now: Optional[float] = None) -> None:
        now = t if now is None else now
        with self._lock:
            st = self.live.setdefault(aid, {"known_names": [], "t": 0})
            fresh = t >= st.get("t", 0) - 1
            if isinstance(rep.get("host"), dict):
                facts = self._facts(rep["host"])
                self.store.execute("UPDATE agent_hosts SET facts=? WHERE id=?", (json.dumps(facts, separators=(",", ":")), aid))
                self._resolve_device(aid, facts)
            dev = self.device_of(aid)
            self._book_flows(aid, dev, rep.get("flows") or [], t, now)
            if not fresh:
                return                          # a buffered report: its bytes count, the current state stays newer
            st["t"] = t
            st["seen"] = now
            if "flows_available" in rep:
                st["flows_available"] = bool(rep["flows_available"])
            for part in ("system", "listen", "units", "overlay"):
                if part in rep:
                    st[part] = rep[part]
            if isinstance(rep.get("containers"), dict) and not rep["containers"].get("bytes_only"):
                self._apply_containers(aid, st, rep["containers"], t)
            self._sample(aid, st, t)
        if now - self._saved > SAVE_EVERY:
            self.save()

    def _apply_containers(self, aid: str, st: dict, part: dict, t: float) -> None:
        old = {c["name"]: c for c in (st.get("containers") or {}).get("items") or []}
        items = []
        for c in (part.get("items") or [])[:ct_mod.MAX_CONTAINERS]:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            c = dict(c)
            prev = old.get(c["name"])
            if prev and prev.get("id") == c.get("id") and c.get("tx") is not None and prev.get("tx") is not None:
                dt = max(1.0, t - float(prev.get("t") or t))
                dtx, drx = c["tx"] - prev["tx"], c["rx"] - prev["rx"]
                c["rate"] = [round(max(0, dtx) / dt), round(max(0, drx) / dt)] if dtx >= 0 and drx >= 0 else [0, 0]
            c["t"] = t
            items.append(c)
        before = json.dumps(sorted((c["name"], c.get("state"), c.get("health")) for c in (st.get("containers") or {}).get("items") or []))
        known = set(st.get("known_names") or [])
        for c in items:
            if not short_lived(c, t) and c["name"] not in known and len(known) < 2000:
                known.add(c["name"])
        st["known_names"] = sorted(known)
        st["containers"] = {"available": bool(part.get("available")), "runtime": part.get("runtime") or "",
                            "version": part.get("version") or "", "reason": part.get("reason"), "items": items}
        after = json.dumps(sorted((c["name"], c.get("state"), c.get("health")) for c in items))
        if before != after:
            self.version += 1

    # ------------------------------------------------------------------ device of a host
    def _resolve_device(self, aid: str, facts: dict) -> Optional[str]:
        core = self.core
        dev = None
        for ip in facts.get("ips") or []:
            dev = core.ip_to_device.get(ip)
            if dev:
                break
        if not dev and facts.get("macs"):
            for d in core.store.devices():
                if (d.get("mac") or "").lower() in facts["macs"]:
                    dev = d["id"]
                    break
        if not dev and facts.get("ips") and facts.get("macs") and not getattr(core, "demo", None):
            dev = self._add_device(facts)
        if dev:
            self.store.execute("UPDATE agent_hosts SET device=? WHERE id=?", (dev, aid))
            self.agents_cache = None
        return dev

    def _add_device(self, facts: dict) -> Optional[str]:
        """A host Lanloom has not scanned yet: it enters the inventory like a scanned one (source "agent")."""
        from . import classify
        ip = next((i for i in facts["ips"] if ":" not in i), "")
        if not ip:
            return None
        host = {"ip": ip, "mac": facts["macs"][0], "hostnames": [facts["name"]] if facts.get("name") else [], "sources": ["agent"],
                "ports": [], "mdns": [], "ssdp": [], "ipv6": [], "wsd": [], "netbios": None, "snmp": None,
                "is_gateway": False, "is_self": False, "rtt_ms": None}
        try:
            self.core.ingest({"hosts": [host]}, None)
            self.core._refresh_index()
            return classify.device_id(host)
        except Exception:
            log.exception("agent host could not be added to the inventory")
            return None

    def device_of(self, aid: str) -> Optional[str]:
        cache = self.agents_cache
        if cache is None:
            cache = self.agents_cache = {r["id"]: r["device"] for r in self.store.query("SELECT id, device FROM agent_hosts")}
        dev = cache.get(aid)
        if dev and self.store.device(dev) is None:
            return None
        return dev

    def _online(self, aid: str, now: float) -> bool:
        st = self.live.get(aid) or {}
        return now - float(st.get("seen") or st.get("t") or 0) < OFFLINE_S

    # ------------------------------------------------------------------ conversations
    def _book_flows(self, aid: str, dev: Optional[str], flows: list, t: float, now: float) -> None:
        if not dev:
            return
        core = self.core
        agent_devs = None
        for f in flows[:MAX_FLOWS]:
            if not isinstance(f, dict):
                continue
            tx, rx = int(scrub.num(f.get("tx"), 0) or 0), int(scrub.num(f.get("rx"), 0) or 0)
            if not (tx or rx):
                continue
            rip = str(f.get("rip") or "")
            peer = core.ip_to_device.get(rip) or getattr(core, "ip6_to_device", {}).get(rip)
            if peer and peer != dev:
                if agent_devs is None:
                    agent_devs = {self.device_of(a) for a in self.live if a != aid and self._online(a, now)}
                if peer in agent_devs and peer < dev:
                    continue                  # the other end runs an agent too and books this conversation
                self._talk(dev, peer, tx, rx, t, now)
            elif not peer and _is_outside(rip) and not getattr(core, "demo", None):
                core.paths.book(dev, rip, tx, rx, t)

    def _talk(self, src: str, dst: str, tx: int, rx: int, t: float, now: float) -> None:
        a, b = (src, dst) if src < dst else (dst, src)
        ab, ba = (tx, rx) if src == a else (rx, tx)
        acc = self.talk_acc.get((a, b))
        if acc is None:
            if len(self.talk_acc) >= 5000:
                return
            acc = self.talk_acc[(a, b)] = [0.0, 0.0, t]
            self.version += 1                     # a new line on the map
        fade = 0.5 ** (max(0.0, t - acc[2]) / TALK_HALF_S)
        acc[0], acc[1], acc[2] = acc[0] * fade + ab, acc[1] * fade + ba, max(acc[2], t)
        book = getattr(self.core.paths, "book_pair", None)    # conversations between devices in the data paths
        if book is not None:
            if ab:
                book(a, b, ab, t)
            if ba:
                book(b, a, ba, t)

    def talk(self, now: Optional[float] = None) -> list:
        """Live conversations between devices seen by agents: [{a, b, ab, ba, src}] (bytes faded over ~10 minutes)."""
        now = self.clock() if now is None else now
        out = []
        for (a, b), (ab, ba, last) in list(self.talk_acc.items()):
            fade = 0.5 ** (max(0.0, now - last) / TALK_HALF_S)
            ab, ba = ab * fade, ba * fade
            if ab + ba < TALK_MIN:
                if now - last > 6 * TALK_HALF_S:
                    self.talk_acc.pop((a, b), None)
                continue
            out.append({"a": a, "b": b, "ab": round(ab), "ba": round(ba), "src": "agent"})
        out.sort(key=lambda p: -(p["ab"] + p["ba"]))
        return out[:MAX_TALK]

    # ------------------------------------------------------------------ samples (host health, forecast feature)
    def _sample(self, aid: str, st: dict, t: float) -> None:
        slot = int(t // SAMPLE_S)
        if self._sampled.get(aid) == slot:
            return
        sys_ = st.get("system") or {}
        if not sys_ and not st.get("containers"):
            return
        self._sampled[aid] = slot
        mem = None
        if sys_.get("mem_total") and sys_.get("mem_avail") is not None:
            mem = round(100.0 * (1 - sys_["mem_avail"] / sys_["mem_total"]), 1)
        disk = None
        for d in sys_.get("disks") or []:
            if d.get("total"):
                used = round(100.0 * (1 - (d.get("free") or 0) / d["total"]), 1)
                disk = used if disk is None else max(disk, used)
        items = visible(st, t)
        facts = {}
        try:
            facts = json.loads((self._row(aid) or {}).get("facts") or "{}")
        except ValueError:
            pass
        self.store.execute("INSERT OR REPLACE INTO agent_samples(slot, agent, load, cpus, mem, disk, failed, containers, unhealthy)"
                           " VALUES(?,?,?,?,?,?,?,?,?)",
                           (slot, aid, (sys_.get("load") or [None])[0], facts.get("cpus"), mem, disk,
                            len((st.get("units") or {}).get("failed") or []), sum(1 for c in items if c.get("state") == "running"),
                            sum(1 for c in items if c.get("health") == "unhealthy")))

    def health(self, aid: str, hours: int = 24, now: Optional[float] = None) -> dict:
        """5-minute samples of one host: [[t, load, cpus, mem %, worst disk %, failed units, running, unhealthy]]."""
        now = self.clock() if now is None else now
        hours = max(1, min(int(hours or 24), KEEP_SAMPLE_DAYS * 24))
        rows = self.store.query("SELECT * FROM agent_samples WHERE agent=? AND slot>=? ORDER BY slot",
                                (str(aid)[:40], int((now - hours * 3600) // SAMPLE_S)))
        return {"agent": aid, "step": SAMPLE_S, "hours": hours,
                "points": [[r["slot"] * SAMPLE_S, r["load"], r["cpus"], r["mem"], r["disk"], r["failed"], r["containers"], r["unhealthy"]]
                           for r in rows]}

    # ------------------------------------------------------------------ views
    def running(self, now: Optional[float] = None) -> dict:
        """The Running view: per host its containers, services and timers (plus failed units and system values)."""
        now = self.clock() if now is None else now
        hosts = []
        for e in self.entries():
            st = self.live.get(e["id"]) or {}
            items = visible(st, now)
            hidden = len((st.get("containers") or {}).get("items") or []) - len(items)
            units = st.get("units") or {}
            hosts.append({**e, "age_s": round(now - st["t"], 1) if st.get("t") else None,
                          "system": st.get("system") or {}, "flows_available": st.get("flows_available", True),
                          "containers": {"available": (st.get("containers") or {}).get("available", False),
                                         "runtime": (st.get("containers") or {}).get("runtime") or "",
                                         "reason": (st.get("containers") or {}).get("reason"),
                                         "items": sorted(items, key=_ct_order), "short_lived": max(0, hidden)},
                          "listen": [s for s in st.get("listen") or [] if not s.get("local")][:300],
                          "listen_local": sum(1 for s in st.get("listen") or [] if s.get("local")),
                          "units": {"available": units.get("available", False), "failed": units.get("failed") or [],
                                    "timers": sorted(units.get("timers") or [], key=lambda x: x.get("next") or 1e18),
                                    "total": units.get("total")},
                          "overlay": st.get("overlay") or {"available": False}})
        return {"hosts": hosts, "server_version": __version__, "image": "%s:%s" % (IMAGE, __version__), "t": round(now, 1),
                "version": self.version}

    def containers_by_device(self, now: Optional[float] = None) -> dict:
        """device id -> [(agent id, container)] of visible containers, for the map's sub-parts of a host."""
        now = self.clock() if now is None else now
        out: dict = {}
        for aid, st in self.live.items():
            dev = self.device_of(aid)
            if not dev:
                continue
            for c in visible(st, now):
                out.setdefault(dev, []).append((aid, c))
        for dev in out:
            out[dev].sort(key=lambda x: _ct_order(x[1]))
        return out

    def container(self, comp_id: str, now: Optional[float] = None) -> Optional[dict]:
        """One container of the map (``ct:<agent>:<name>``) for its card."""
        try:
            _, aid, name = comp_id.split(":", 2)
        except ValueError:
            return None
        st = self.live.get(aid) or {}
        c = next((c for c in (st.get("containers") or {}).get("items") or [] if c.get("name") == name), None)
        if c is None:
            return None
        row = self._row(aid) or {}
        return {"container": c, "agent": aid, "host": row.get("name") or "", "device": self.device_of(aid),
                "runtime": (st.get("containers") or {}).get("runtime") or "", "age_s": round((now or self.clock()) - st.get("t", 0), 1)}

    def device_detail(self, dev_id: str, now: Optional[float] = None) -> Optional[dict]:
        """What the agents know about one device, for its card: one entry of the Running view."""
        ids = [aid for aid in self.live if self.device_of(aid) == dev_id]
        if not ids:
            return None
        run = self.running(now)
        hosts = [h for h in run["hosts"] if h["id"] in ids]
        return {"hosts": hosts, "server_version": run["server_version"], "image": run["image"]} if hosts else None

    def summary(self, now: Optional[float] = None) -> dict:
        """Counts for the overview, the nightly report and the palette."""
        now = self.clock() if now is None else now
        out = {"agents": 0, "online": 0, "proxies": 0, "containers": 0, "unhealthy": 0, "failed_units": 0, "failed_timers": 0,
               "update": 0}
        for e in self.entries():
            out["agents" if e["kind"] == "agent" else "proxies"] += 1
            out["online"] += e["online"]
            out["update"] += bool(e["update"])
            st = self.live.get(e["id"]) or {}
            items = visible(st, now)
            out["containers"] += sum(1 for c in items if c.get("state") == "running")
            out["unhealthy"] += sum(1 for c in items if c.get("health") == "unhealthy" or c.get("state") == "restarting")
            units = st.get("units") or {}
            out["failed_units"] += len(units.get("failed") or [])
            out["failed_timers"] += sum(1 for x in units.get("timers") or [] if timer_failed(x))
        return out

    # ------------------------------------------------------------------ background: proxies, events, saving
    def poll_proxies(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        for r in self._rows():
            if r["kind"] != "proxy" or not r["url"]:
                continue
            st = self.live.setdefault(r["id"], {"known_names": [], "t": 0})
            try:
                rt = self._runtimes.get(r["id"])
                if rt is None or rt.url != r["url"]:
                    rt = self._runtimes[r["id"]] = ct_mod.Runtime(r["url"])
                    rt.version()
                part = {"available": True, "runtime": rt.kind, "items": rt.containers()}
                st.pop("error", None)
                with self._lock:
                    st["t"] = st["seen"] = now
                    self._apply_containers(r["id"], st, scrub.strip(part), now)
                    self._sample(r["id"], st, now)
                self.store.execute("UPDATE agent_hosts SET last_seen=? WHERE id=?", (now, r["id"]))
                if not self.device_of(r["id"]):
                    self._resolve_device(r["id"], json.loads(r["facts"] or "{}"))
            except ct_mod.SourceError as exc:
                st["error"] = exc.key
            except Exception:
                log.exception("container source %s failed", r["id"])
                st["error"] = "agent.err.containers_unreachable"

    def watch(self, now: Optional[float] = None) -> None:
        """One watch round (every minute): announce offline agents, failed units and unhealthy containers."""
        now = self.clock() if now is None else now
        notify = getattr(self.core, "notify", None)
        if notify is None:
            return
        rows = {r["id"]: r for r in self._rows()}
        current = set()
        for aid, st in list(self.live.items()):
            row = rows.get(aid)
            if row is None:
                continue
            dev = self.device_of(aid)
            name = row["name"]
            if row["kind"] == "agent" and row["last_seen"] and now - row["last_seen"] > OFFLINE_S:
                current.add((aid, "offline"))
            if not self._online(aid, now):
                continue
            for u in (st.get("units") or {}).get("failed") or []:
                current.add((aid, "unit:" + str(u.get("name"))[:120]))
            for x in (st.get("units") or {}).get("timers") or []:
                if timer_failed(x):
                    current.add((aid, "unit:" + str(x.get("unit") or x.get("name"))[:120]))
            for c in visible(st, now):
                if c.get("health") == "unhealthy" or c.get("state") == "restarting":
                    current.add((aid, "ct:" + c["name"]))
        for key in current - set(self._events):
            aid, what = key
            row, dev = rows[aid], self.device_of(aid)
            data = {"host": row["name"]}
            try:
                if what == "offline":
                    notify.emit("agent_offline", "agent_offline:%s" % aid, data, device=dev, t=now)
                elif what.startswith("unit:"):
                    notify.emit("host_unit_failed", "host_unit_failed:%s:%s" % (aid, what[5:]), dict(data, unit=what[5:]), device=dev, t=now)
                else:
                    notify.emit("container_unhealthy", "container_unhealthy:%s:%s" % (aid, what[3:]), dict(data, container=what[3:]),
                                device=dev, t=now)
            except Exception:
                log.exception("agent event failed")
            self._events[key] = True
        for key in set(self._events) - current:
            aid, what = key
            eid = "agent_offline" if what == "offline" else "host_unit_failed" if what.startswith("unit:") else "container_unhealthy"
            k = "agent_offline:%s" % aid if what == "offline" else "%s:%s:%s" % (eid, aid, what.split(":", 1)[1])
            try:
                notify.emit(eid, k, resolve=True, t=now)
            except Exception:
                log.exception("agent event failed")
            self._events.pop(key, None)

    def prune(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        self.store.execute("DELETE FROM agent_samples WHERE slot < ?", (int((now - KEEP_SAMPLE_DAYS * 86400) // SAMPLE_S),))

    def save(self) -> None:
        self._saved = self.clock()
        for aid, st in list(self.live.items()):
            keep = {k: v for k, v in st.items() if k in ("system", "listen", "units", "overlay", "containers", "known_names",
                                                         "flows_available")}
            self.store.execute("UPDATE agent_hosts SET state=? WHERE id=?", (json.dumps(keep, separators=(",", ":")), aid))

    async def loop(self) -> None:
        last_watch = last_prune = 0.0
        while True:
            try:
                now = self.clock()
                if not getattr(self.core, "demo", None) and any(r["kind"] == "proxy" for r in self._rows()):
                    await asyncio.to_thread(self.poll_proxies, now)
                if now - last_watch >= 60:
                    last_watch = now
                    self.watch(now)
                    self.save()
                if now - last_prune >= 6 * 3600:
                    last_prune = now
                    self.prune(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("agent loop failed")
            await asyncio.sleep(PROXY_EVERY)


# ---------------------------------------------------------------------- helpers shared with the views
def short_lived(c: dict, now: float) -> bool:
    """True for a container that lived less than SHORT_S (see the module docstring)."""
    created = c.get("created") or c.get("started")
    if not created:
        return False
    if c.get("state") in ("running", "restarting", "paused"):
        return now - float(created) < SHORT_S
    end = c.get("finished") or now
    return float(end) - float(created) < SHORT_S


def visible(st: dict, now: float) -> list:
    """Containers of one host after the noise filter."""
    known = set(st.get("known_names") or [])
    out = []
    for c in (st.get("containers") or {}).get("items") or []:
        if c.get("name") in known or c.get("state") == "restarting" or c.get("health") == "unhealthy" or not short_lived(c, now):
            out.append(c)
    return out


def timer_failed(x: dict) -> bool:
    r = str(x.get("unit_result") or x.get("result") or "success")
    return r not in ("success", "") or x.get("unit_state") == "failed"


_STATE_ORDER = {"restarting": 0, "running": 1, "paused": 2, "created": 3, "exited": 4, "dead": 5}


def _ct_order(c: dict):
    bad = 0 if c.get("health") == "unhealthy" or c.get("state") == "restarting" else 1
    return (bad, _STATE_ORDER.get(c.get("state"), 9), (c.get("project") or ""), c.get("name") or "")


def container_component(aid: str, host_dev: str, c: dict, rate_text=None) -> dict:
    """A container as a sub-part of its host on the network map. A container that was stopped on purpose (exit code
    0) is grey, not red: only a failed exit, a dead or restarting container counts as down."""
    state = c.get("state")
    failed = state in ("dead", "restarting") or (state == "exited" and (c.get("exit_code") or 0) != 0)
    status = "up" if state == "running" else "down" if failed else "unknown"
    grade = "interrupted" if c.get("health") == "unhealthy" or state == "restarting" else "periodic" if state == "running" else "unknown"
    image = str(c.get("image") or "")
    short = image.rsplit("/", 1)[-1][:40]
    comp = {"id": "ct:%s:%s" % (aid, c["name"]), "kind": "container", "title": c["name"], "subtitle": short, "status": status,
            "grade": grade, "metric": None, "badges": [], "fold": host_dev}
    if c.get("project"):
        comp["badges"] = [c["project"][:16]]
    if rate_text and state == "running" and c.get("rate") and sum(c["rate"]) > 0:
        comp["metric"] = rate_text(sum(c["rate"]))
    return comp


# ---------------------------------------------------------------------- notification events (N1 register)
def _has_agents(core) -> bool:
    hub = getattr(core, "agents", None)
    try:
        return bool(hub and hub._rows())
    except Exception:
        return False


from .notify.events import make as _make, register as _register  # noqa: E402

_register(_make("agent_offline", "hosts", 60, "warning", hold=True, recovery=True, available=_has_agents))
_register(_make("host_unit_failed", "hosts", 61, "warning", recovery=True, available=_has_agents))
_register(_make("container_unhealthy", "hosts", 62, "warning", hold=True, recovery=True, available=_has_agents))
