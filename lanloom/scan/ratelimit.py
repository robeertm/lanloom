"""Rate limiting for outgoing probes.

* `TokenBucket`  - global limit of new connections (or datagrams) per second.
* `Limiter`      - token bucket + global concurrency + per-host concurrency.

Clock and sleep are injectable so the logic can be tested without waiting.
"""
from __future__ import annotations

import asyncio
import contextlib
import time


class TokenBucket:
    """Classic token bucket. `acquire()` waits until one token is available."""

    def __init__(self, rate: float, burst: float | None = None, clock=time.monotonic, sleep=asyncio.sleep):
        if not rate or rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate)
        self.capacity = float(burst if burst is not None else max(1.0, min(rate, 10.0)))
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = asyncio.Lock()
        self.granted = 0

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1.0 - 1e-9:   # tolerance: float rounding must not stall the loop
                    self._tokens = max(0.0, self._tokens - 1.0)
                    self.granted += 1
                    return
                await self._sleep(max((1.0 - self._tokens) / self.rate, 1e-4))


class Limiter:
    """Combines the global token bucket with global and per-host concurrency limits."""

    def __init__(self, rate: float = 40.0, concurrency: int = 24, per_host: int = 4,
                 clock=time.monotonic, sleep=asyncio.sleep):
        self.bucket = TokenBucket(rate, clock=clock, sleep=sleep)
        self.concurrency = max(1, int(concurrency))
        self.per_host = max(1, int(per_host))
        self._global = asyncio.Semaphore(self.concurrency)
        self._hosts: dict[str, asyncio.Semaphore] = {}
        self._active: dict[str, int] = {}
        self.max_active_global = 0
        self.max_active_host = 0
        self._active_total = 0

    def _host_sem(self, host: str) -> asyncio.Semaphore:
        sem = self._hosts.get(host)
        if sem is None:
            sem = self._hosts[host] = asyncio.Semaphore(self.per_host)
        return sem

    @property
    def probes(self) -> int:
        return self.bucket.granted

    @contextlib.asynccontextmanager
    async def slot(self, host: str):
        """Hold one connection slot for `host`; the token is taken after both semaphores."""
        host_sem = self._host_sem(host)
        async with host_sem:
            async with self._global:
                await self.bucket.acquire()
                self._active[host] = self._active.get(host, 0) + 1
                self._active_total += 1
                self.max_active_host = max(self.max_active_host, self._active[host])
                self.max_active_global = max(self.max_active_global, self._active_total)
                try:
                    yield
                finally:
                    self._active[host] -= 1
                    self._active_total -= 1
                    if not self._active[host]:
                        self._active.pop(host, None)
