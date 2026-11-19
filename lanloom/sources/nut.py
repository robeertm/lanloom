"""Read-only UPS source: the Network UPS Tools protocol (TCP 3493), the variables of every UPS a server offers.

Only these commands are ever sent (``_ALLOWED``): ``LIST UPS``, ``LIST VAR <ups>``, ``VERSION`` and ``LOGOUT``. There is
no ``USERNAME``/``PASSWORD``/``LOGIN`` and no ``INSTCMD``/``SET``, so a connection can neither switch an outlet nor start
a battery test; the protocol of an anonymous client cannot write anything anyway, and this client makes sure of it
for itself. Replies are read line by line with hard limits (line length, number of variables, total time).

Every ``poll_s`` seconds the source emits one ``{"type": "nut", ...}`` event with a neutral reading per UPS (see
``lanloom/controlroom/ups.py`` for the neutral form and the meaning of the status words).
"""
from __future__ import annotations

import asyncio
import re

from . import _common
from ._common import SourceError, StatusTracker, clip, emit_event, error_key, now, short_test

MODULE = "nut"
SOURCE = "nut"
PORT = 3493
POLL_S = 15.0
MAX_LINE = 1024
MAX_VARS = 400
MAX_UPS = 16
READ_TIMEOUT_S = 6.0
_ALLOWED = re.compile(rb"\A(?:LIST UPS|LIST VAR [A-Za-z0-9_.@-]{1,64}|VERSION|LOGOUT)\n\Z")
_NAME = re.compile(r"\A[A-Za-z0-9_.@-]{1,64}\Z")
_VAR = re.compile(r'\AVAR ([A-Za-z0-9_.@-]+) ([A-Za-z0-9_.-]+) "(.*)"\Z')
_UPS = re.compile(r'\AUPS ([A-Za-z0-9_.@-]+) "(.*)"\Z')
# variables kept (everything else a server offers is dropped here, so a device cannot fill the book with junk)
KEEP = ("ups.status", "ups.load", "ups.temperature", "ups.realpower", "ups.power", "ups.beeper.status", "ups.test.result",
        "ups.model", "ups.mfr", "ups.firmware", "ups.delay.shutdown", "ups.alarm",
        "battery.charge", "battery.charge.low", "battery.charge.warning", "battery.runtime", "battery.runtime.low",
        "battery.voltage", "battery.voltage.nominal", "battery.temperature", "battery.date", "battery.type",
        "input.voltage", "input.voltage.nominal", "input.frequency", "output.voltage", "output.frequency", "device.model", "device.mfr")


def valid_host(host) -> str:
    host = str(host or "").strip()
    if not host or len(host) > 253 or any(c.isspace() or ord(c) < 32 for c in host):
        raise SourceError("source.nut.config", "address missing or invalid")
    return host


def parse_lines(lines: list) -> dict:
    """{variable: value} from ``VAR <ups> <name> "<value>"`` lines; unknown names and broken lines are skipped."""
    out = {}
    for line in lines[:MAX_VARS * 2]:
        m = _VAR.match(line)
        if m and m.group(2) in KEEP and len(out) < MAX_VARS:
            out[m.group(2)] = clip(m.group(3).replace('\\"', '"'), 120)
    return out


def number(value):
    try:
        v = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) < 1e9 else None


def neutral(name: str, description, v: dict) -> dict:
    """The reading of one UPS in the neutral form the book keeps."""
    return {"name": clip(name, 64), "description": clip(description, 80) or "", "status": clip(v.get("ups.status"), 40) or "",
            "charge": number(v.get("battery.charge")), "charge_low": number(v.get("battery.charge.low")),
            "runtime_s": number(v.get("battery.runtime")), "runtime_low_s": number(v.get("battery.runtime.low")),
            "load": number(v.get("ups.load")), "input_v": number(v.get("input.voltage")), "output_v": number(v.get("output.voltage")),
            "battery_v": number(v.get("battery.voltage")), "temp": number(v.get("ups.temperature") or v.get("battery.temperature")),
            "power_w": number(v.get("ups.realpower")), "test": clip(v.get("ups.test.result"), 60) or "",
            "model": clip(v.get("ups.model") or v.get("device.model"), 60) or ""}


class _Conn:
    """One session: send an allowed command, read until the matching END line."""

    def __init__(self, reader, writer):
        self.r, self.w = reader, writer

    async def ask(self, command: str, end: str) -> list:
        data = (command + "\n").encode("ascii", "replace")
        if not _ALLOWED.match(data):
            raise SourceError("source.nut.protocol", "command not allowed")      # a programming error, never a device reply
        self.w.write(data)
        await self.w.drain()
        lines = []
        while True:
            raw = await asyncio.wait_for(self.r.readline(), READ_TIMEOUT_S)
            if not raw:
                raise SourceError("source.nut.closed", "connection closed")
            if len(raw) > MAX_LINE:
                raise SourceError("source.nut.protocol", "line too long")
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("ERR "):
                code = line[4:40]
                raise SourceError("source.nut.refused" if code.startswith("ACCESS") else "source.nut.protocol", "server error")
            if line == "END " + end:
                return lines
            if line == "BEGIN " + end or not line:
                continue
            lines.append(line)
            if len(lines) > MAX_VARS * 2:
                raise SourceError("source.nut.protocol", "too many lines")

    async def close(self) -> None:
        try:
            self.w.write(b"LOGOUT\n")
            await asyncio.wait_for(self.w.drain(), 1.0)
        except Exception:
            pass
        try:
            self.w.close()
        except Exception:
            pass


class Nut:
    """Live source: reads every UPS of one NUT server (or only ``ups`` when a name is set) every ``interval`` seconds."""

    name = SOURCE

    def __init__(self, host, port=PORT, ups="", interval=POLL_S, timeout=5.0):
        self.host = valid_host(host)
        try:
            self.port = int(port or PORT)
        except (TypeError, ValueError):
            raise SourceError("source.nut.config", "port invalid")
        if not 1 <= self.port <= 65535:
            raise SourceError("source.nut.config", "port invalid")
        self.ups = str(ups or "").strip()
        if self.ups and not _NAME.match(self.ups):
            raise SourceError("source.nut.config", "UPS name invalid")
        self.interval = max(5.0, float(interval))
        self.timeout = float(timeout)

    def __repr__(self):
        return "Nut(%s:%d)" % (self.host, self.port)

    __str__ = __repr__

    async def _open(self) -> _Conn:
        r, w = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        return _Conn(r, w)

    async def read_all(self) -> dict:
        """One session -> the nut event (units: neutral readings)."""
        conn = await self._open()
        try:
            names = []
            for line in await conn.ask("LIST UPS", "LIST UPS"):
                m = _UPS.match(line)
                if m and (not self.ups or m.group(1) == self.ups) and len(names) < MAX_UPS:
                    names.append((m.group(1), m.group(2)))
            units = []
            for name, descr in names:
                lines = await conn.ask("LIST VAR " + name, "LIST VAR " + name)
                units.append(neutral(name, descr, parse_lines(lines)))
        finally:
            await conn.close()
        if not units:
            raise SourceError("source.nut.no_ups", "no UPS offered")
        return {"type": "nut", "source": SOURCE, "host": self.host, "t": now(), "units": units}

    async def run(self, emit) -> None:
        status = StatusTracker(SOURCE)
        backoff = _common.Backoff()
        while True:
            try:
                good = None
                while True:
                    await emit_event(emit, await self.read_all())
                    await status.set(emit, True, "source.nut.ok")
                    good = good or asyncio.get_running_loop().time()
                    if asyncio.get_running_loop().time() - good >= 30:
                        backoff.reset()
                    await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await status.set(emit, False, error_key(MODULE, exc))
            await asyncio.sleep(backoff.next())

    async def test(self):
        async def probe():
            ev = await self.read_all()
            return {"ups": len(ev["units"]), "names": [u["name"] for u in ev["units"]][:8]}
        return await short_test(MODULE, probe(), self.timeout * 4 + READ_TIMEOUT_S)
