"""Shared helpers for the live sources: reconnect backoff, status events, emitting.

Every source follows the same contract:

* ``__init__(**cfg)`` validates its configuration and keeps secrets in private attributes only;
* ``async run(emit)`` loops until cancelled, reconnects with exponential backoff (1 s -> 60 s) and emits
  ``{"type": "status", "source": name, "ok": bool, "key": reason_key, "t": ts}`` whenever its state changes;
* ``async test() -> (ok, key, details)`` performs one short connection attempt for the setup wizard.

``run()`` never raises anything but ``asyncio.CancelledError``. Reason keys are i18n keys; status events never
carry exception texts, so nothing secret can leak through them.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time

log = logging.getLogger("lanloom.sources")

BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 60.0
HEALTHY_AFTER_S = 30.0       # a session that lasted this long resets the backoff


class SourceError(Exception):
    """Expected failure of a source. ``key`` is an i18n reason key; the text never contains secrets."""

    def __init__(self, key: str, text: str = ""):
        super().__init__(text or key)
        self.key = key


class Backoff:
    """Exponential reconnect delay: 1, 2, 4, ... 60 s. ``reset()`` after a healthy session."""

    def __init__(self, start: float = BACKOFF_START_S, maximum: float = BACKOFF_MAX_S):
        self.start = float(start)
        self.maximum = float(maximum)
        self._next = self.start

    def next(self) -> float:
        delay = self._next
        self._next = min(self.maximum, self._next * 2)
        return delay

    def reset(self) -> None:
        self._next = self.start


def now() -> float:
    return round(time.time(), 3)


async def emit_event(emit, event: dict) -> None:
    """Call ``emit`` (plain function or coroutine function). A failing consumer never kills a source."""
    try:
        result = emit(event)
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception as exc:      # consumer bug: log the class only, never the event content
        log.warning("emit failed: %s", type(exc).__name__)


class StatusTracker:
    """Emits a status event only when (ok, key) changes."""

    def __init__(self, source: str):
        self.source = source
        self.last: tuple[bool, str] | None = None

    async def set(self, emit, ok: bool, key: str) -> None:
        if self.last == (ok, key):
            return
        self.last = (ok, key)
        await emit_event(emit, {"type": "status", "source": self.source, "ok": bool(ok), "key": key, "t": now()})


def error_key(module: str, exc: BaseException) -> str:
    """Map an exception to a reason key without looking at its (possibly secret-bearing) text."""
    if isinstance(exc, SourceError):
        return exc.key
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return "source.%s.timeout" % module
    if isinstance(exc, ConnectionRefusedError):
        return "source.%s.refused" % module
    if isinstance(exc, OSError):
        import ssl
        if isinstance(exc, ssl.SSLError | ssl.CertificateError):
            return "source.%s.tls" % module
        return "source.%s.unreachable" % module
    return "source.%s.error" % module


async def run_with_backoff(source: str, module: str, emit, session, *, backoff: Backoff | None = None) -> None:
    """Generic reconnect loop. ``session(emit, status)`` runs one connection and returns or raises when it ends.

    ``status`` is the StatusTracker; the session sets ``ok=True`` itself once it is connected."""
    status = StatusTracker(source)
    backoff = backoff or Backoff()
    while True:
        started = time.monotonic()
        try:
            await session(emit, status)
            key = "source.%s.closed" % module
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            key = error_key(module, exc)
            log.info("%s: session ended (%s)", source, key)
        if time.monotonic() - started >= HEALTHY_AFTER_S:
            backoff.reset()
        await status.set(emit, False, key)
        await asyncio.sleep(backoff.next())


async def short_test(module: str, coro, timeout: float):
    """Run a test coroutine returning details; map failures to (False, key, {})."""
    try:
        details = await asyncio.wait_for(coro, timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return False, error_key(module, exc), {}
    return True, "source.%s.ok" % module, details


def clip(value, limit: int = 255):
    """Strings from devices are untrusted: force str, strip control characters, cap length."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    if len(text) > limit:
        text = text[:limit]
    return "".join(ch for ch in text if ch >= " " or ch == "\t")
