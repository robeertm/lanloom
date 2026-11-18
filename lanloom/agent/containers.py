"""Container source: the containers of a host from its container runtime (the common runtime API), read only.

Used by the host agent (local socket) and by Lanloom itself for a socket proxy on another host. The client can only
send GET requests - ``_get`` has no method parameter and a test checks that no other HTTP method appears in this
file - so even a socket with full rights is only read. A socket proxy that allows GET and nothing else is the
stricter choice and needs no change here.

What is kept per container (positive list, everything else is dropped right after parsing, before anything is
stored or sent): short id, name, image, state, health, created/started/finished times, exit code, restart count,
published ports, network names and addresses, network mode, compose project/service and the byte counters of the
container's network. Environment variables, command line, entry point, labels other than the compose names, mounts
and health check output are never copied.

Bytes per container come from the counters of the container's network namespace (``/proc/<pid>/net/dev``, works
when the agent shares the host's process namespace) and otherwise from the runtime's one-shot statistics. A container
on the host network has no counters of its own and reports none.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import time
from datetime import datetime
from urllib.parse import urlsplit

from . import scrub

SOCKETS = ("/var/run/docker.sock", "/run/podman/podman.sock", "/run/docker.sock")  # identifier: runtime socket paths
MAX_CONTAINERS = 400
INSPECT_EVERY = 300
TIMEOUT = 5.0
STATES = ("running", "exited", "restarting", "paused", "created", "dead", "removing")
_HEALTH = re.compile(r"\((healthy|unhealthy|health: starting)\)")
_EXIT = re.compile(r"Exited \((-?\d+)\)")


class SourceError(Exception):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def find_socket() -> str:
    env = os.environ.get("LANLOOM_AGENT_CONTAINERS", "").strip()
    if env:
        return env
    for p in SOCKETS:
        if os.path.exists(p):
            return "unix://" + p
    return ""


def normalise_url(url: str) -> str:
    """unix:///path, http://host:port or tcp://host:port (-> http). "" when the value is not one of these."""
    url = (url or "").strip()
    if url.startswith("/"):
        url = "unix://" + url
    if url.startswith("tcp://"):
        url = "http://" + url[6:]
    parts = urlsplit(url)
    if parts.scheme == "unix" and parts.path.startswith("/") and ".." not in parts.path:
        return "unix://" + parts.path
    if parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password:
        return "%s://%s%s" % (parts.scheme, parts.hostname if ":" not in parts.hostname else "[%s]" % parts.hostname,
                              ":%d" % parts.port if parts.port else "")
    return ""


class Runtime:
    """Read-only client of one container runtime API."""

    def __init__(self, url: str, timeout: float = TIMEOUT, proc: str = "/proc", clock=time.time):
        self.url = normalise_url(url)
        if not self.url:
            raise SourceError("agent.err.containers_url")
        self.timeout = timeout
        self.proc = proc
        self.clock = clock
        self.kind = ""
        self._inspect: dict = {}            # id -> (when, state, facts)

    def _conn(self):
        p = urlsplit(self.url)
        if p.scheme == "unix":
            return _UnixConnection(p.path, self.timeout)
        if p.scheme == "https":
            import ssl
            return http.client.HTTPSConnection(p.hostname, p.port or 443, timeout=self.timeout, context=ssl.create_default_context())
        return http.client.HTTPConnection(p.hostname, p.port or 80, timeout=self.timeout)

    def _get(self, path: str):
        """GET one API path and decode the JSON answer. The only request this client can make."""
        conn = self._conn()
        try:
            conn.request("GET", path, headers={"Accept": "application/json", "User-Agent": "lanloom-agent"})
            resp = conn.getresponse()
            raw = resp.read(32 << 20)
            if resp.status in (401, 403):
                raise SourceError("agent.err.containers_refused")
            if resp.status != 200:
                raise SourceError("agent.err.containers_status")
            return json.loads(raw.decode("utf-8", "replace"))
        except (OSError, http.client.HTTPException) as exc:
            raise SourceError("agent.err.containers_unreachable") from exc
        except ValueError as exc:
            raise SourceError("agent.err.containers_answer") from exc
        finally:
            conn.close()

    def version(self) -> dict:
        v = self._get("/version")
        comps = " ".join(str(c.get("Name", "")) for c in v.get("Components") or [] if isinstance(c, dict)).lower()
        self.kind = "podman" if "podman" in comps else "docker"  # identifier: runtime names
        return {"runtime": self.kind, "version": scrub.text(v.get("Version"), 32), "api": scrub.text(v.get("ApiVersion"), 16)}

    def _facts(self, cid: str, state: str) -> dict:
        hit = self._inspect.get(cid)
        now = self.clock()
        if hit and hit[1] == state and now - hit[0] < INSPECT_EVERY:
            return hit[2]
        raw = self._get("/containers/%s/json" % cid)
        st = raw.get("State") or {}
        facts = {"pid": scrub.num(st.get("Pid")) or 0, "started": _ts(st.get("StartedAt")), "finished": _ts(st.get("FinishedAt")),
                 "exit_code": scrub.num(st.get("ExitCode")), "restarts": scrub.num(raw.get("RestartCount")) or 0,
                 "health": ((st.get("Health") or {}).get("Status") or None)}
        del raw                                        # environment, command line, mounts: dropped right here
        self._inspect[cid] = (now, state, facts)
        return facts

    def _bytes(self, cid: str, pid: int, netmode: str) -> tuple:
        if netmode == "host":
            return None, None
        if pid:
            got = netns_bytes(pid, self.proc)
            if got is not None:
                return got
        try:
            st = self._get("/containers/%s/stats?stream=false&one-shot=true" % cid)
        except SourceError:
            return None, None
        nets = st.get("networks") or {}
        if not isinstance(nets, dict) or not nets:
            return None, None
        tx = sum(int(n.get("tx_bytes") or 0) for n in nets.values() if isinstance(n, dict))
        rx = sum(int(n.get("rx_bytes") or 0) for n in nets.values() if isinstance(n, dict))
        return tx, rx

    def containers(self, with_bytes: bool = True) -> list:
        rows = self._get("/containers/json?all=1")
        if not isinstance(rows, list):
            raise SourceError("agent.err.containers_answer")
        out, alive = [], set()
        for r in rows[:MAX_CONTAINERS]:
            if not isinstance(r, dict):
                continue
            item = parse_list_row(r)
            if not item:
                continue
            alive.add(item["id"])
            try:
                f = self._facts(item["id"], item["state"])
            except SourceError:
                f = {}
            for k in ("started", "finished", "exit_code", "restarts"):
                if f.get(k) is not None:
                    item[k] = f[k]
            if f.get("health"):
                item["health"] = scrub.text(f["health"], 16)
            if with_bytes and item["state"] == "running":
                tx, rx = self._bytes(item["id"], f.get("pid") or 0, item.get("netmode", ""))
                if tx is not None:
                    item["tx"], item["rx"] = tx, rx
            out.append(item)
        self._inspect = {k: v for k, v in self._inspect.items() if k in alive}
        return out


def _ts(value):
    """Runtime time stamp (RFC 3339 with nanoseconds) -> epoch seconds; the zero time -> None."""
    if not value or not isinstance(value, str) or value.startswith("0001-"):
        return None
    s = re.sub(r"(\.\d{1,6})\d*", r"\1", value.strip()).replace("Z", "+00:00")
    try:
        return round(datetime.fromisoformat(s).timestamp(), 1)
    except ValueError:
        return None


def parse_list_row(r: dict) -> dict | None:
    """One row of the runtime's container list -> the kept fields (see the module docstring)."""
    cid = str(r.get("Id") or "")[:12]
    if not re.fullmatch(r"[0-9a-f]{12}", cid):
        return None
    names = r.get("Names") or []
    name = (names[0] if names else cid).lstrip("/")
    state = str(r.get("State") or "").lower()
    if state not in STATES:
        state = "unknown"
    status = str(r.get("Status") or "")
    item = {"id": cid, "name": scrub.text(name, 80), "image": scrub.text(r.get("Image"), 120), "state": state,
            "created": scrub.num(r.get("Created"))}
    m = _HEALTH.search(status)
    if m:
        item["health"] = "starting" if m.group(1) == "health: starting" else m.group(1)
    m = _EXIT.search(status)
    if m:
        item["exit_code"] = int(m.group(1))
    ports = []
    for p in r.get("Ports") or []:
        if isinstance(p, dict) and p.get("PrivatePort"):
            ports.append({"target": scrub.num(p.get("PrivatePort")), "port": scrub.num(p.get("PublicPort")),
                          "proto": scrub.text(p.get("Type") or "tcp", 4)})
    if ports:
        seen, uniq = set(), []
        for p in ports:
            k = (p["target"], p["port"], p["proto"])
            if k not in seen:
                seen.add(k)
                uniq.append(p)
        item["ports"] = uniq[:32]
    nets = ((r.get("NetworkSettings") or {}).get("Networks")) or {}
    if isinstance(nets, dict) and nets:
        item["networks"] = [{"name": scrub.text(k, 64), "ip": scrub.text((v or {}).get("IPAddress") or "", 45)}
                            for k, v in list(nets.items())[:8]]
    mode = ((r.get("HostConfig") or {}).get("NetworkMode")) or ""
    if mode:
        item["netmode"] = "host" if mode == "host" else scrub.text(mode, 32)
    item.update(scrub.labels(r.get("Labels")))
    return item


def netns_bytes(pid: int, proc: str = "/proc") -> tuple | None:
    """(tx, rx) of all interfaces but loopback in the network namespace of ``pid``; None when it cannot be read."""
    try:
        with open("%s/%d/net/dev" % (proc, int(pid)), "r", encoding="ascii", errors="replace") as fh:
            text = fh.read(1 << 16)
    except (OSError, ValueError):
        return None
    tx = rx = 0
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() == "lo":
            continue
        f = rest.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
    return tx, rx


def collect(url: str = "", runtime: Runtime | None = None) -> tuple:
    """(report part, runtime) for the agent; never raises. ``runtime`` is kept between calls (inspect cache)."""
    url = url or find_socket()
    if not url:
        return {"available": False, "reason": "no_socket"}, None
    try:
        rt = runtime if runtime is not None and runtime.url == normalise_url(url) else Runtime(url)
        info = rt.version() if not rt.kind else {"runtime": rt.kind}
        items = rt.containers()
        return dict(info, available=True, items=items), rt
    except SourceError as exc:
        return {"available": False, "reason": exc.key}, runtime
