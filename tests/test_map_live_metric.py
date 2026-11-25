"""The live metric on a box of the map: a measured cable gives the box at its lower end its rate (#21)."""
import time
from pathlib import Path
from types import SimpleNamespace

from lanloom import core, live, mapmodel
from lanloom.store import Store


def bare():
    c = core.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=False), Store(":memory:"), None)
    c.ip_to_device = {"192.168.50.2": "d-sw", "192.168.50.20": "d-office"}
    return c


def test_a_cable_rate_shows_on_the_box_at_its_lower_end_only():
    _, metrics = mapmodel.live_overlay(bare(), time.time(), {"p:d-sw:d-office": [1_000_000, 0]})
    assert metrics.get("d-office") and "d-sw" not in metrics


def test_a_flow_still_shows_on_both_ends():
    _, metrics = mapmodel.live_overlay(bare(), time.time(), {"f:d-sw:d-office": [1_000_000, 0]})
    assert metrics.get("d-office") and metrics.get("d-sw")


def test_a_box_with_its_own_flows_and_a_measured_cable_never_shows_both_added():
    c = bare()
    _, cable = mapmodel.live_overlay(c, time.time(), {"p:d-sw:d-office": [1_000_000, 0]})
    _, both = mapmodel.live_overlay(c, time.time(), {"p:d-sw:d-office": [1_000_000, 0], "f:d-sw:d-office": [400_000, 0]})
    assert both["d-office"] == cable["d-office"]                 # the cable already carries the box's own flows


# ---------------------------------------------------------------------------------------------- received and sent apart (lanloom#26)
def split_core():
    c = bare()
    c.update_settings({"rate_split": True})
    return c


def test_split_is_off_by_default_and_the_text_stays_as_before():
    c = bare()
    assert c.rate_split() is False
    _, metrics = mapmodel.live_overlay(c, time.time(), {"p:d-sw:d-office": [1_000_000, 500_000]})
    assert metrics["d-office"] == "1.5 MB/s"


def test_split_cable_reads_received_at_the_lower_end():
    _, metrics = mapmodel.live_overlay(split_core(), time.time(), {"p:d-sw:d-office": [1_000_000, 0]})
    assert metrics["d-office"] == "↓1.0 ↑0 MB/s"        # direction 0 is parent -> child: the child receives it
    assert "d-sw" not in metrics


def test_split_flow_shows_the_same_trace_mirrored_at_both_ends():
    _, metrics = mapmodel.live_overlay(split_core(), time.time(), {"f:d-sw:d-office": [1_000_000, 0]})
    assert metrics["d-sw"] == "↓0 ↑1.0 MB/s"             # a sends what direction 0 carries
    assert metrics["d-office"] == "↓1.0 ↑0 MB/s"


def test_split_internet_and_router_read_from_the_view_of_the_network():
    c = split_core()
    c.ip_to_device["192.168.50.1"] = "d-router"
    c.gateway_ip = "192.168.50.1"
    _, metrics = mapmodel.live_overlay(c, time.time(), {"u:internet:d-router": [2_000_000, 100_000]})
    assert metrics["internet"] == metrics["d-router"] == "↓2.0 ↑0.1 MB/s"


def test_split_measured_cable_wins_over_smaller_flows_but_never_adds():
    c = split_core()
    _, m = mapmodel.live_overlay(c, time.time(), {"p:d-sw:d-office": [1_000_000, 0], "f:d-sw:d-office": [400_000, 0]})
    assert m["d-office"] == "↓1.0 ↑0 MB/s"


def test_split_container_tile_uses_its_pair_as_sent_and_received():
    from lanloom.agents import container_component
    unit = "bytes"
    comp = container_component("a", "d-host", {"name": "web", "state": "running", "rate": [100_000, 2_000_000]},
                               lambda down, up: live.human_rate_split(down, up, unit))
    assert comp["metric"] == "↓2.0 ↑0.1 MB/s"        # rate is [tx, rx]: rx is received
