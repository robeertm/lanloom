"""Performance budget (B4): server time of the main APIs, the live loop and memory at 250 and 1000 devices.

CPU speed differs between hosts, so times are judged as a ratio to a fixed pure-Python workload measured in the same
run (scripts/benchmark.py: ``calib_ms``); memory is judged in megabytes (Python heap of the core, tracemalloc). The limits
are about three times what one core of a small arm64 single-board computer measured (README "Hardware requirements"), so they catch regressions
- a quadratic loop, a cache that stopped working, a per-request re-read - without flaking on a busy machine."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("lanloom_benchmark", Path(__file__).resolve().parent.parent / "scripts" / "benchmark.py")
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)

# ratio to calib_ms at 250 devices
LIMITS_250 = {"start_ms": 3, "tick_ms": 4, "map_overview_ms": 6, "map_network_ms": 9, "devices_ms": 5, "component_ms": 5,
              "paths_24h_ms": 7, "paths_30d_ms": 8, "storage_ms": 2, "search_ms": 4, "overview_ms": 6, "inventory_ms": 4,
              "automations_ms": 8,      # Automations tab: list, who switches what, timeline, day plan of as many automations as devices
              "failure_ms": 6,
              "rewind_ms": 20,          # rewind of 24 hours: a day of minutes on every trace (finished days are kept encoded)
              "incidents_ms": 9}        # incidents computed again over the slots of every device
# growth from 250 to 1000 devices (linear = 4, quadratic = 16; 8 leaves room for a busy machine and the garbage collector)
GROWTH = {"tick_ms": 8, "map_overview_ms": 8, "map_network_ms": 8, "devices_ms": 8, "component_ms": 8, "search_ms": 8, "overview_ms": 8, "inventory_ms": 8, "automations_ms": 8, "failure_ms": 8, "rewind_ms": 8, "incidents_ms": 8}
HEAP_MB = {250: 15, 1000: 45}


@pytest.fixture(scope="module")
def results():
    bench.BEST = True                      # fastest of several runs: a busy machine slows single runs, not all of them
    try:
        calib = min(bench.calib(), bench.calib())
        return calib, bench.measure(250, runs=4), bench.measure(1000, runs=3)
    finally:
        bench.BEST = False


def test_main_apis_within_budget_at_250_devices(results):
    calib, r, _ = results
    over = {k: round(r[k] / calib, 2) for k, lim in LIMITS_250.items() if r[k] / calib > lim}
    if over:
        # a busy test machine (other jobs start and stop) can slow one stretch of the run: measure once more with a fresh
        # calibration right before; a key fails only when both runs are over its limit (the limits stay as they are)
        bench.BEST = True
        try:
            calib2 = min(bench.calib(), bench.calib())
            r2 = bench.measure(250, runs=4)
        finally:
            bench.BEST = False
        over = {k: (v, round(r2[k] / calib2, 2)) for k, v in over.items() if r2[k] / calib2 > LIMITS_250[k]}
    assert not over, "over budget twice (x calibration): %s; calib %.1f ms, measured %s" % (over, calib, r)


def test_growth_to_1000_devices_stays_near_linear(results):
    _, r250, r1000 = results
    bad = {k: round(r1000[k] / max(0.01, r250[k]), 2) for k, lim in GROWTH.items() if r1000[k] / max(0.01, r250[k]) > lim}
    if bad:
        # as for the API budget: a busy test machine can slow one stretch of the run, so both sizes are measured once
        # more; a key fails only when the growth is over its limit in both runs (the limits stay as they are)
        bench.BEST = True
        try:
            a, b = bench.measure(250, runs=4), bench.measure(1000, runs=3)
        finally:
            bench.BEST = False
        bad = {k: (v, round(b[k] / max(0.01, a[k]), 2)) for k, v in bad.items() if b[k] / max(0.01, a[k]) > GROWTH[k]}
    assert not bad, "grows faster than allowed from 250 to 1000 devices, twice: %s" % bad


def test_memory_of_the_core(results):
    _, r250, r1000 = results
    assert r250["heap_mb"] <= HEAP_MB[250], r250
    assert r1000["heap_mb"] <= HEAP_MB[1000], r1000
    # live rings: 24 bytes a sample; 1000 traces x 10 minutes stay well under 20 MB
    assert r1000["ring_samples"] * 24 / 1e6 < 20


def test_map_payload_stays_small(results):
    _, r250, r1000 = results
    assert r250["map_kb"] < 150 and r1000["map_kb"] < 600
