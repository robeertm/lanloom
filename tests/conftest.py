"""Shared test setup.

Hang guard: a single test that runs longer than LANLOOM_TEST_HANG_S seconds (default 300) prints the stacks of all
threads and ends the whole run with exit code 3. Without it a stuck socket, event loop or thread would block CI
until the job timeout without any hint where it hangs.
"""
import faulthandler
import os
import sys
import threading

import pytest

HANG_S = float(os.environ.get("LANLOOM_TEST_HANG_S", "300"))


def _hang_guard(config, nodeid: str, limit: float):
    def fire():
        # pytest captures fd 2 during a test; give it back first, or the report dies with the process
        capman = config.pluginmanager.getplugin("capturemanager")
        try:
            if capman is not None:
                capman.suspend_global_capture(in_=True)
        except Exception:
            pass
        sys.stderr.write("\n!!! test hangs for more than %.0f s: %s\n" % (limit, nodeid))
        sys.stderr.flush()
        faulthandler.dump_traceback(all_threads=True)
        sys.stderr.flush()
        os._exit(3)
    timer = threading.Timer(limit, fire)
    timer.daemon = True
    return timer


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    timer = _hang_guard(item.config, item.nodeid, HANG_S)
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
