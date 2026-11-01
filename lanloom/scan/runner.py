"""Scan plan validation and the scan pipeline.

Phases (progress callback receives {"phase","done","total","found"}):
  neighbors -> mdns + ssdp (concurrently) -> ports -> banners
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import ipaddress
import logging
import socket
import time

from . import banners, mdns, neighbors, netinfo, ports, safety, ssdp
from .ratelimit import Limiter, TokenBucket

log = logging.getLogger("lanloom.scan")

PLAN_VERSION = 1
R_PLAN_INVALID = "scan.plan.invalid"

# option -> (gentle default, deep default, min, max). Overrides may only stay within [min, max].
OPTIONS: dict[str, tuple] = {
    "rate": (40.0, 40.0, 1.0, 100.0),
    "concurrency": (24, 24, 1, 64),
    "per_host": (4, 4, 1, 8),
    "connect_timeout": (0.8, 1.0, 0.1, 5.0),
    "nudge_rate": (neighbors.NUDGE_RATE, neighbors.NUDGE_RATE, 1.0, 200.0),
    "settle_s": (neighbors.SETTLE_S, neighbors.SETTLE_S, 0.0, 5.0),
    "mdns_s": (3.0, 3.0, 0.0, 10.0),
    "ssdp_s": (3.0, 3.0, 0.0, 10.0),
    "banner_timeout": (2.0, 2.0, 0.2, 5.0),
    "rdns_timeout": (1.0, 1.0, 0.0, 5.0),
}
SWITCHES = ("neighbors", "mdns", "ssdp", "banners", "rdns")
MAX_HOSTNAMES = 8


def make_plan(cidrs, deep: bool = False, consent: bool = False, **overrides) -> dict:
    """Validate ranges and options and return a JSON-serialisable plan.

    Raises ValueError(reason_key) when anything is unsafe or invalid.
    """
    deep = bool(deep)
    if deep and consent is not True:
        raise ValueError(safety.R_CONSENT)
    ok, key, nets = safety.check_plan_ranges(list(cidrs) if isinstance(cidrs, (list, tuple)) else cidrs, deep)
    if not ok:
        raise ValueError(key)
    plan = {"version": PLAN_VERSION, "mode": "deep" if deep else "gentle", "consent": bool(consent),
            "cidrs": [str(n) for n in nets],
            "ports": sorted(ports.DEEP_PORTS if deep else ports.GENTLE_PORTS)}
    for name, (g, d, _lo, _hi) in OPTIONS.items():
        plan[name] = d if deep else g
    for name in SWITCHES:
        plan[name] = True
    for name, value in overrides.items():
        if name in OPTIONS:
            lo, hi = OPTIONS[name][2], OPTIONS[name][3]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                raise ValueError(R_PLAN_INVALID)
            plan[name] = type(OPTIONS[name][0])(value)
        elif name in SWITCHES:
            if not isinstance(value, bool):
                raise ValueError(R_PLAN_INVALID)
            plan[name] = value
        else:
            raise ValueError(R_PLAN_INVALID)
    return plan


def validate_plan(plan) -> list:
    """Re-check a plan (it may have been stored/edited). Returns the networks or raises ValueError."""
    if not isinstance(plan, dict) or plan.get("version") != PLAN_VERSION:
        raise ValueError(R_PLAN_INVALID)
    deep = plan.get("mode") == "deep"
    if plan.get("mode") not in ("gentle", "deep"):
        raise ValueError(R_PLAN_INVALID)
    if deep and plan.get("consent") is not True:
        raise ValueError(safety.R_CONSENT)
    ok, key, nets = safety.check_plan_ranges(plan.get("cidrs"), deep)
    if not ok:
        raise ValueError(key)
    allowed = ports.DEEP_PORTS if deep else ports.GENTLE_PORTS
    plist = plan.get("ports")
    if not isinstance(plist, list) or not plist or any(not isinstance(p, int) or p not in allowed for p in plist):
        raise ValueError(R_PLAN_INVALID)
    for name, (_g, _d, lo, hi) in OPTIONS.items():
        v = plan.get(name)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(R_PLAN_INVALID)
    for name in SWITCHES:
        if not isinstance(plan.get(name), bool):
            raise ValueError(R_PLAN_INVALID)
    return nets


class _Progress:
    """Throttled, exception-safe progress reporting (sync or async callbacks)."""

    def __init__(self, cb, min_interval: float = 0.1):
        self.cb = cb
        self.min_interval = min_interval
        self.last: dict[str, float] = {}
        self.tasks: set = set()

    def __call__(self, phase: str, done: int, total: int, found: int, force: bool = False) -> None:
        if self.cb is None:
            return
        now = time.monotonic()
        if not force and now - self.last.get(phase, 0.0) < self.min_interval:
            return
        self.last[phase] = now
        try:
            res = self.cb({"phase": phase, "done": int(done), "total": int(total), "found": int(found)})
            if inspect.isawaitable(res):
                task = asyncio.ensure_future(res)
                self.tasks.add(task)
                task.add_done_callback(self._done)
        except Exception:  # a broken callback must never break the scan
            log.debug("progress callback failed", exc_info=True)

    def _done(self, task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.debug("progress callback failed")


class _Host:
    __slots__ = ("ip", "mac", "hostnames", "sources", "open", "mdns", "ssdp", "rtt")

    def __init__(self, ip: str):
        self.ip = ip
        self.mac = None
        self.hostnames: list[str] = []
        self.sources: list[str] = []
        self.open: dict[int, dict | None] = {}
        self.mdns: list[dict] = []
        self.ssdp: list[dict] = []
        self.rtt: float | None = None

    def add_source(self, s: str) -> None:
        if s not in self.sources:
            self.sources.append(s)

    def add_name(self, name) -> None:
        if not isinstance(name, str):
            return
        name = name.strip().rstrip(".")[:253]
        if not name or name == self.ip or len(self.hostnames) >= MAX_HOSTNAMES:
            return
        if name.lower() not in (h.lower() for h in self.hostnames):
            self.hostnames.append(name)

    def add_rtt(self, rtt) -> None:
        if rtt is not None and (self.rtt is None or rtt < self.rtt):
            self.rtt = rtt


def _sort_key(ip: str):
    return ipaddress.IPv4Address(ip)


async def _reverse_names(ips, timeout: float) -> dict[str, str]:
    if timeout <= 0 or not ips:
        return {}
    loop = asyncio.get_running_loop()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="lanloom-rdns")
    out: dict[str, str] = {}

    async def one(ip):
        try:
            host, _ = await asyncio.wait_for(
                loop.run_in_executor(pool, socket.getnameinfo, (ip, 0), socket.NI_NAMEREQD), timeout)
            if host and host != ip:
                out[ip] = host
        except (OSError, asyncio.TimeoutError, UnicodeError, ValueError):
            pass

    try:
        await asyncio.gather(*(one(ip) for ip in ips))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


async def run(plan, progress=None) -> dict:
    """Execute a plan. Returns a ScanResult dict. Cancellable."""
    nets = validate_plan(plan)
    started = time.time()
    t0 = time.monotonic()
    prog = _Progress(progress)
    deep = plan["mode"] == "deep"

    # ---------------------------------------------------------------- networks
    try:
        local = netinfo.detect_networks()
    except Exception:
        log.debug("network detection failed", exc_info=True)
        local = []
    result_networks = []
    on_link = []
    for item in local:
        try:
            lnet = ipaddress.ip_network(item["cidr"])
        except (KeyError, ValueError):
            continue
        if any(lnet.overlaps(n) for n in nets):
            result_networks.append(item)
            on_link.append(lnet)
    covered = [n for n in nets if any(n.overlaps(l) for l in on_link)]
    for n in nets:
        if not any(n.overlaps(ipaddress.ip_network(i["cidr"])) for i in result_networks):
            result_networks.append({"cidr": str(n), "iface": None, "self_ip": None, "gateway": None,
                                    "private": True, "size": n.num_addresses, "host_mode": False})
    self_ips = {i["self_ip"] for i in result_networks if i.get("self_ip")}
    gateways = {i["gateway"] for i in result_networks if i.get("gateway")}
    iface_ips = sorted(self_ips)

    hosts: dict[str, _Host] = {}

    def host(ip: str) -> _Host:
        h = hosts.get(ip)
        if h is None:
            h = hosts[ip] = _Host(ip)
        return h

    def in_scope(ip) -> bool:
        return safety.is_scannable_ip(ip, nets)

    limiter = Limiter(plan["rate"], plan["concurrency"], plan["per_host"])
    extra_probes = 0
    timeout = float(plan["connect_timeout"])

    # ---------------------------------------------------------------- phase: neighbours
    all_addrs = [a for n in nets for a in safety.host_addresses(n)]
    linked_addrs = [a for n in covered for a in safety.host_addresses(n)]
    prog("neighbors", 0, len(all_addrs), 0, force=True)
    arp = neighbors.read_arp() if plan["neighbors"] else None
    if plan["neighbors"] and arp is not None and linked_addrs:
        bucket = TokenBucket(plan["nudge_rate"])
        sent = 0
        for i in range(0, len(linked_addrs), 32):
            chunk = [a for a in linked_addrs[i:i + 32] if a not in self_ips]
            sent += await neighbors.nudge(chunk, bucket)
            prog("neighbors", min(i + 32, len(linked_addrs)), len(all_addrs), len(hosts))
        extra_probes += sent
        await asyncio.sleep(plan["settle_s"])
        arp = neighbors.read_arp() or {}
    for ip, entry in (arp or {}).items():
        if in_scope(ip):
            h = host(ip)
            h.mac = entry["mac"]
            h.add_source("arp")

    # liveness sweep where the neighbour table cannot help (routed ranges / no /proc)
    linked_set = set(linked_addrs)
    sweep_addrs = [a for a in all_addrs if a not in hosts and (arp is None or a not in linked_set)]
    if plan["neighbors"] and sweep_addrs:
        done = 0

        async def sweep_one(ip):
            nonlocal done
            for port in ports.SWEEP_PORTS:
                async with limiter.slot(ip):
                    state, rtt = await ports.tcp_probe(ip, port, timeout)
                if state in (ports.OPEN, ports.REFUSED):
                    h = host(ip)
                    h.add_source("tcp")
                    h.add_rtt(rtt)
                    break
            done += 1
            prog("neighbors", len(all_addrs) - len(sweep_addrs) + done, len(all_addrs), len(hosts))

        await _bounded(sweep_addrs, sweep_one, plan["concurrency"])
    prog("neighbors", len(all_addrs), len(all_addrs), len(hosts), force=True)

    # ---------------------------------------------------------------- phases: mDNS + SSDP
    async def do_mdns():
        if not plan["mdns"] or plan["mdns_s"] <= 0:
            prog("mdns", 1, 1, 0, force=True)
            return {}, 0
        prog("mdns", 0, 100, 0, force=True)
        res, sent = await mdns.browse(plan["mdns_s"], networks=nets, iface_ips=iface_ips,
                                      progress=lambda f, n: prog("mdns", int(f * 100), 100, n))
        prog("mdns", 100, 100, len(res), force=True)
        return res, sent

    async def do_ssdp():
        if not plan["ssdp"] or plan["ssdp_s"] <= 0:
            prog("ssdp", 1, 1, 0, force=True)
            return {}, 0
        prog("ssdp", 0, 100, 0, force=True)
        res, sent = await ssdp.search(plan["ssdp_s"], networks=nets, iface_ips=iface_ips,
                                      progress=lambda f, n: prog("ssdp", int(f * 100), 100, n))
        prog("ssdp", 100, 100, len(res), force=True)
        return res, sent

    (mres, msent), (sres, ssent) = await asyncio.gather(_safe(do_mdns(), ({}, 0)), _safe(do_ssdp(), ({}, 0)))
    extra_probes += msent + ssent
    for ip, info in mres.items():
        if not in_scope(ip):
            continue
        h = host(ip)
        h.add_source("mdns")
        for svc in info["services"]:
            if svc not in h.mdns and len(h.mdns) < 64:
                h.mdns.append(svc)
        for name in sorted(info["hostnames"]):
            h.add_name(name)
    for ip, entries in sres.items():
        if not in_scope(ip):
            continue
        h = host(ip)
        h.add_source("ssdp")
        h.ssdp.extend(entries[:16])

    for ip in self_ips | gateways:
        if in_scope(ip):
            host(ip)

    # ---------------------------------------------------------------- phase: ports (+ reverse DNS)
    targets = sorted(hosts, key=_sort_key)
    port_list = list(plan["ports"])
    jobs = [(ip, p) for p in port_list for ip in targets]      # port-major: spreads load across hosts
    total = len(jobs)
    done_ports = 0
    prog("ports", 0, total, len(hosts), force=True)

    async def probe(job):
        nonlocal done_ports
        ip, port = job
        async with limiter.slot(ip):
            state, rtt = await ports.tcp_probe(ip, port, timeout)
        h = hosts[ip]
        if state == ports.OPEN:
            h.open[port] = None
            h.add_source("tcp")
            h.add_rtt(rtt)
        elif state == ports.REFUSED:
            h.add_source("tcp")
            h.add_rtt(rtt)
        done_ports += 1
        prog("ports", done_ports, total, len(hosts))

    rdns_task = None
    if plan["rdns"] and targets:
        rdns_task = asyncio.ensure_future(_safe(_reverse_names(targets, plan["rdns_timeout"]), {}))
    try:
        await _bounded(jobs, probe, plan["concurrency"])
        prog("ports", total, total, len(hosts), force=True)
        names = await rdns_task if rdns_task else {}
    finally:
        if rdns_task and not rdns_task.done():
            rdns_task.cancel()
    for ip, name in names.items():
        if ip in hosts:
            hosts[ip].add_name(name)

    # ---------------------------------------------------------------- phase: banners
    web_jobs = [(ip, p) for ip in targets for p in sorted(hosts[ip].open) if p in ports.WEB_PORTS]
    if not plan["banners"]:
        web_jobs = []
    done_b = 0
    prog("banners", 0, len(web_jobs), len(hosts), force=True)

    async def grab(job):
        nonlocal done_b
        ip, port = job
        async with limiter.slot(ip):
            b = await banners.fetch_banner(ip, port, ports.WEB_PORTS[port], timeout=plan["banner_timeout"])
        hosts[ip].open[port] = b
        done_b += 1
        prog("banners", done_b, len(web_jobs), len(hosts))

    await _bounded(web_jobs, grab, plan["concurrency"])
    prog("banners", len(web_jobs), len(web_jobs), len(hosts), force=True)

    # ---------------------------------------------------------------- result
    finished = time.time()
    duration = time.monotonic() - t0
    out_hosts = []
    for ip in sorted(hosts, key=_sort_key):
        h = hosts[ip]
        out_hosts.append({
            "ip": ip, "mac": h.mac, "hostnames": list(h.hostnames), "sources": list(h.sources),
            "ports": [{"port": p, "proto": "tcp", "service": ports.service_name(p), "banner": h.open[p]}
                      for p in sorted(h.open)],
            "mdns": h.mdns, "ssdp": h.ssdp,
            "is_gateway": ip in gateways, "is_self": ip in self_ips,
            "rtt_ms": round(h.rtt, 2) if h.rtt is not None else None,
        })
    probes = limiter.probes + extra_probes
    return {"started": started, "finished": finished, "mode": "deep" if deep else "gentle",
            "networks": result_networks, "hosts": out_hosts,
            "stats": {"probes": probes, "rate_per_s": round(probes / duration, 1) if duration > 0 else 0.0,
                      "duration_s": round(duration, 2)}}


async def _safe(coro, fallback):
    """Run a phase; any error except cancellation yields `fallback` (the scan continues)."""
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("scan phase failed", exc_info=True)
        return fallback


async def _bounded(items, fn, workers: int) -> None:
    """Run fn(item) for all items with at most `workers` concurrent tasks; errors are contained."""
    if not items:
        return
    queue: asyncio.Queue = asyncio.Queue()
    for it in items:
        queue.put_nowait(it)

    async def worker():
        while True:
            try:
                it = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await fn(it)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("probe failed", exc_info=True)

    tasks = [asyncio.ensure_future(worker()) for _ in range(max(1, min(workers, len(items))))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
