"""The agent's loop: read the host every few seconds, send one report every ``REPORT_S`` seconds.

Cadence: TCP counters every 2 s (summed into the report), containers every 30 s, everything else once a minute, the
host facts every 10 minutes. Each collector is fenced: one that fails reports ``available: false`` and the rest goes on.
"""
from __future__ import annotations

import logging
import time

from .. import __version__
from . import PROTO, REPORT_S, containers, dbus, overlay, sockets, system

log = logging.getLogger("lanloom.agent")

POLL_S = 2.0
CONTAINERS_S = 30
MINUTE_S = 60
FACTS_S = 600


class Collector:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.meter = sockets.FlowMeter()
        self.runtime = None
        self.listen: list = []
        self.due = {"containers": 0.0, "minute": 0.0, "facts": 0.0}
        self.last_report = clock()

    def poll(self) -> None:
        try:
            self.meter.poll()
        except Exception:                      # a broken reading never stops the agent
            log.exception("agent: reading TCP counters failed")

    def report(self, full: bool = False) -> dict:
        now = self.clock()
        rep = {"t": round(now, 1), "interval": round(max(1.0, now - self.last_report), 1), "flows": self.meter.take()}
        self.last_report = now
        if self.meter.available is False:
            rep["flows_available"] = False
        if full or now >= self.due["facts"]:
            self.due["facts"] = now + FACTS_S
            rep["host"] = self._fenced(system.facts, {})
        if full or now >= self.due["minute"]:
            self.due["minute"] = now + MINUTE_S
            rep["system"] = self._fenced(system.sample, {})
            self.listen = self._fenced(sockets.listening, self.listen)
            self.meter.listen_ports = sockets.ports_of(self.listen)
            rep["listen"] = self.listen
            rep["units"] = self._fenced(dbus.units, {"available": False})
            rep["overlay"] = self._fenced(overlay.collect, {"available": False})
        if full or now >= self.due["containers"]:
            self.due["containers"] = now + CONTAINERS_S
            try:
                part, self.runtime = containers.collect(runtime=self.runtime)
            except Exception:
                log.exception("agent: reading containers failed")
                part = {"available": False, "reason": "error"}
            rep["containers"] = part
        return rep

    @staticmethod
    def _fenced(fn, fallback):
        try:
            return fn()
        except Exception:
            log.exception("agent: %s failed", getattr(fn, "__name__", "collector"))
            return fallback


def _idle(stop, sleep, code: int) -> int:
    """Wait without sending until the agent is stopped. Exiting would only make the container runtime restart it in a
    loop; a new pairing needs a new code anyway (and with it a new start)."""
    while not stop():
        sleep(POLL_S)
    return code


def run(link, code: str = "", stop=lambda: False, sleep=time.sleep) -> int:
    """Pair when needed, then report until ``stop()``. Returns an exit code."""
    col = Collector()
    if code and not link.used_code(code):
        try:
            ans = link.pair(code, system.facts(), PROTO)
            log.info("agent: paired with %s as %s", link.server, ans.get("agent_id"))
        except Exception as exc:
            if not link.paired:
                log.error("agent: pairing failed (%s)", getattr(exc, "key", type(exc).__name__))
                return _idle(stop, sleep, 2)
            log.warning("agent: new code not accepted (%s), keeping the existing pairing", getattr(exc, "key", type(exc).__name__))
    if not link.paired:
        log.error("agent: not paired. Start it once with LANLOOM_AGENT_CODE set to a code from Lanloom > Settings > Agents.")
        return _idle(stop, sleep, 2)
    log.info("agent %s reporting to %s every %d s", __version__, link.server, REPORT_S)
    col.poll()
    next_report = time.time() + REPORT_S
    backoff = REPORT_S
    while not stop():
        sleep(POLL_S)
        col.poll()
        if time.time() < next_report:
            continue
        link.queue(col.report())
        state = link.flush(PROTO)
        if state == "revoked":
            log.error("agent: stopped sending (token revoked). Give it a new code to pair again.")
            link.forget()
            return _idle(stop, sleep, 3)
        backoff = REPORT_S if state in ("ok", "empty") else min(backoff * 2, 300)
        next_report = time.time() + (REPORT_S if state in ("ok", "empty") else backoff)
    link.save_buffer(force=True)
    return 0
