"""The live metric on a box of the map: a measured cable gives the box at its lower end its rate (#21)."""
import time
from pathlib import Path
from types import SimpleNamespace

from lanloom import core, mapmodel
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
