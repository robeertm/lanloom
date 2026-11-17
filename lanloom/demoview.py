"""A sample network next to the real one: "look at a sample first" from the setup assistant.

The sample is a second core with its own in-memory store, filled with the fictional demo network (the same data as
``LANLOOM_DEMO=1``). A browser session that switched it on carries the cookie ``ll_demo``; for such a request the
app answers every core-backed read (map, devices, topology, data paths, night reports, live feed) from the sample.
It never touches the real inventory and never sends a packet: the demo core only runs its synthetic live loop.

Leaving is one click (the cookie goes away). A demo view may not change anything except leaving and signing out;
every other state-changing request is refused with ``demo.err.read_only``. The sample core stops after
``IDLE_S`` seconds without a request.
"""
from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import logging
import os
import time
import types

log = logging.getLogger("lanloom.demoview")

COOKIE = "ll_demo"
ACTIVE = contextvars.ContextVar("lanloom_demo_view", default=False)
IDLE_S = 900
WRITABLE = {"/api/setup/demo", "/api/logout"}   # the only state-changing calls a demo view may make


def _demo_cfg(cfg):
    if dataclasses.is_dataclass(cfg):
        return dataclasses.replace(cfg, demo=True, data_dir=None, probe=False)
    out = types.SimpleNamespace(**vars(cfg))
    out.demo, out.data_dir, out.probe = True, None, False
    return out


class DemoView:
    def __init__(self, cfg):
        self.cfg = cfg
        self.core = None
        self.last = 0.0
        self._tasks: list = []
        self._lock = None

    def requested(self, cookies) -> bool:
        return not getattr(self.cfg, "demo", False) and cookies.get(COOKIE) == "1"

    async def ensure(self):
        """The running sample core (built on first use)."""
        self.last = time.time()
        if self.core is not None:
            return self.core
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self.core is None:
                self.core = await asyncio.to_thread(self._build)
                self._tasks = [asyncio.create_task(self.core.demo.run(self.core)),
                               asyncio.create_task(self.core._tick_loop()),
                               asyncio.create_task(self._idle())]
                log.info("sample network started for a demo view")
        return self.core

    def _build(self):
        from .core import Core
        from .crypto import KEY_BYTES, Vault
        from .demo import Demo
        from .store import Store
        store = Store(":memory:")
        core = Core(_demo_cfg(self.cfg), store, Vault(store, os.urandom(KEY_BYTES)))
        core.demo = Demo()
        core.ai = None
        core.audit = None
        core.demo.seed(core)
        from .inventory_demo import seed as seed_inventory
        seed_inventory(core)
        core.demo.seed_integrations(core)
        core.demo.seed_reports(core)
        from .notify.demo import seed as seed_notify
        seed_notify(core)
        store.set_setting("setup_done", True)
        core.geo.start()
        core.oui.start()
        return core

    async def _idle(self) -> None:
        while True:
            await asyncio.sleep(60)
            if time.time() - self.last > IDLE_S:
                asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.stop()))
                return

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        current = asyncio.current_task()
        for t in tasks:
            if t is not current:
                t.cancel()
        for t in tasks:
            if t is current:
                continue
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self.core is not None:
            log.info("sample network stopped")
        self.core = None
