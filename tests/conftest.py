"""Shared test setup.

Hang guard: a single test that runs longer than LANLOOM_TEST_HANG_S seconds (default 300) prints the stacks of all
threads and ends the whole run with exit code 3. Without it a stuck socket, event loop or thread would block CI
until the job timeout without any hint where it hangs.

The guard watches progress, not the clock: a long test that is still working (the performance budget measures one API
after the other, several minutes on a busy test machine) reports each finished step through the ``test_progress``
fixture, and the limit counts again from there. A stuck test reports nothing and still ends after the limit.
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


_RUNNING: dict = {}       # the guard of the test that runs right now: config, nodeid, timer


def progress() -> None:
    """The running test made progress: its hang guard counts the limit from now on."""
    run = _RUNNING.get("run")
    if not run:
        return
    old = run["timer"]
    run["timer"] = _hang_guard(run["config"], run["nodeid"], HANG_S)
    run["timer"].start()
    old.cancel()


@pytest.fixture(scope="session")
def test_progress():
    """Call the returned function after each finished step of a long test (see the module docstring)."""
    return progress


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    run = {"config": item.config, "nodeid": item.nodeid, "timer": _hang_guard(item.config, item.nodeid, HANG_S)}
    _RUNNING["run"] = run
    run["timer"].start()
    try:
        yield
    finally:
        _RUNNING.pop("run", None)
        run["timer"].cancel()
