"""Circuit-board router: every visible trace is routed octilinearly from pad to pad, never through a tile, fast enough."""
from _board_node import run_node

ROUTE_BUDGET_MS = 3000   # generous for CI machines; the reference board (120 tiles / 200 traces) routes in ~0.5 s on a desktop


def test_router_routes_everything_cleanly():
    out = run_node("router_test.js")
    for case in out["cases"]:
        assert case["routed"] == case["nets"], case
        assert case["failed"] == 0, case
        assert case["issues"] == [], case
    big = [c for c in out["cases"] if c["n"] == 120]
    assert all(c["nets"] >= 200 for c in big)
    assert all(c["ms"] < ROUTE_BUDGET_MS for c in big), big


def test_compact_rail_routes():
    out = run_node("router_test.js")
    for case in out["rail"]:
        assert case["routed"] == case["nets"], case
        assert case["issues"] == [], case
        assert case["ms"] < 200, case


def test_cache_key_and_worker_protocol():
    out = run_node("router_test.js")
    assert out["cache"]["same"]
    assert out["cache"]["key1"] == out["cache"]["key2"]
    assert out["cache"]["keyChanges"]
    w = out["worker"]
    assert w["done"] and w["all"] == w["nets"] and w["parts"] >= 1 and w["single"]
    assert out["fallback"]["octilinear"]


def test_dot_decomposition_conserves_quanta():
    out = run_node("router_test.js")
    for d in out["decompose"]:
        assert d["conserved"], d
        assert d["withinCap"], d


def test_core_api_and_bundle_groups():
    out = run_node("router_test.js")
    assert out["api"]["api"] == 1 and out["api"]["version"] == 6 and out["api"]["names"] == [], out["api"]
    g = out["groups"]
    assert g["routed"] == 5 and g["failed"] == 0 and g["issues"] == [], g
    assert g["sharedPads"] < g["separatePads"] == 5, g   # members share the pad at their common target, separate nets never do


def test_rim_nodes_and_worker_grids():
    out = run_node("router_test.js")
    r = out["rim"]
    assert r["padRim"] and abs(r["padX"] - 220) <= 6 and r["oct"] and r["turned"], r
    assert out["workerGrids"] == {"keptBefore": True, "droppedAfter": True}


def test_build_nets_with_host_vocabulary():
    n = run_node("router_test.js")["nets"]
    assert n["n"] == 2 and n["first"] is True and n["both"] is True, n
    assert n["kind"] == "sync" and n["data"] == 2 and n["hits"] == [True], n
