"""S4: the notification event and the nightly section of unstable switch ports."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lanloom import core, portfront, portnotify
from lanloom.demo_live import live_sample
from lanloom.nightly.contrib import REGISTRY, Ctx
from lanloom.notify import events as ev
from lanloom.store import Store


class FakeNotify:
    def __init__(self):
        self.calls = []

    def emit(self, eid, key, data=None, resolve=False, t=None, **kw):
        self.calls.append((eid, key, dict(data or {}), resolve))


@pytest.fixture()
def c():
    k = core.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=False), Store(":memory:"), None)
    k.notify = FakeNotify()
    st = live_sample(1_800_000_000)
    k.live_cfg[5] = {"state": st, "caps": {}, "family": "x", "t": time.time()}
    k.integrations = lambda: [{"id": 5, "name": "Sample", "kind": "controller", "enabled": True}]
    return k


def test_the_event_is_registered_with_texts_and_an_all_clear():
    e = ev.get("port_unstable")
    assert e and e.recovery and e.severity == "warning"


def test_a_port_turning_red_is_announced_once_and_cleared_when_calm(c):
    now = time.time()
    pid = "p_e_sw1_12"
    c.portwatch.events[pid] = [[now - 100, "flap"], [now - 90, "flap"]]
    portnotify.check(c, now)
    assert c.notify.calls == []                                           # two events are only yellow
    c.portwatch.events[pid].append([now - 10, "err"])
    portnotify.check(c, now)
    assert c.notify.calls == [("port_unstable", "port:" + pid, {"name": "Switch hall · Port 12", "n": 3}, False)]
    portnotify.check(c, now + 1)                                          # still red: the notifier itself keeps it to one announcement
    c.portwatch.events[pid] = []
    portnotify.check(c, now + 2)
    assert c.notify.calls[-1] == ("port_unstable", "port:" + pid, {}, True)


def test_the_nightly_section_lists_ports_with_events_in_the_period(c):
    now = time.time()
    c.portwatch.events["p_e_sw1_12"] = [[now - 3600 * k, "flap"] for k in (1, 2, 3)]
    c.portwatch.events["p_e_sw1_7"] = [[now - 1800, "err"], [now - 3600 * 30, "err"]]        # the second is outside the period
    c.roamwatch.moves = [[now - 600, "a", "b"], [now - 700, "b", "a"]]
    sec = next(x for x in REGISTRY if x.id == "ports")
    ctx = Ctx(core=c, journal=None, since=now - 86400, until=now, prev=None)
    assert sec.available(ctx)
    out = sec.build(ctx)
    keys = [(i["key"], i["point"], i["vars"].get("port")) for i in out["items"]]
    assert ("report.ports.port", True, "Port 12") in keys and ("report.ports.port", False, "Port 7") in keys
    assert any(k[0] == "report.ports.roam" for k in keys)
    assert out["summary"]["key"] == "report.ports.summary" and out["summary"]["vars"]["n"] == 2 and out["level"] == "danger"


def test_a_calm_night_says_so(c):
    now = time.time()
    c.portwatch.feed([{"id": "p", "link": True}], now - 100)
    out = next(x for x in REGISTRY if x.id == "ports").build(Ctx(core=c, journal=None, since=now - 86400, until=now, prev=None))
    assert out["summary"]["key"] == "report.ports.none" and out["level"] == "ok"
