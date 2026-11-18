"""Nightly report section "Zone matrix" (U4): baseline on the first night, zone pairs that opened or closed since, whole
zones open to the internet; only when a connection reads zones and rules. Fixed clock, texts in every language of the app.
Sample data only (zone names, no addresses)."""
import asyncio
import copy
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from lanloom import core as core_mod
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.firewall import report as _fw_report  # noqa: F401 - registers the section (the app imports it too)
from lanloom.textfmt import languages
from lanloom.nightly import Reports, render
from lanloom.store import Store

TZ = "Europe/Amsterdam"
Z = ZoneInfo(TZ)


def at(y, mo, d, hh=0, mm=0):
    return datetime(y, mo, d, hh, mm, tzinfo=Z).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def pol(pid, src, dst, action="allow", order=10000, **kw):
    return dict({"id": pid, "name": pid, "src": {"zone": src}, "dst": {"zone": dst}, "action": action, "proto": "any",
                 "ports": "", "enabled": True, "order": order}, **kw)


STATE = {"zones": [{"id": "z_int", "name": "Internal", "system": "internal", "builtin": True},
                   {"id": "z_ext", "name": "External", "system": "external", "builtin": True},
                   {"id": "z_iot", "name": "IoT", "builtin": False}],
         "networks": [{"id": "n1", "name": "Main", "zone": "z_int", "cidr": "192.168.50.0/24"},
                      {"id": "n2", "name": "Things", "zone": "z_iot", "cidr": "192.168.60.0/24"}],
         "devices": [],
         "policies": [pol("int-out", "z_int", "z_ext"), pol("iot-out", "z_iot", "z_ext", order=10001)]}


def make(tmp_path, clock, states):
    store = Store(":memory:")
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=False, probe=False,
                          release_feed="", image_repo="", cookie_secure="never", trusted_origins=(), trusted_proxies=())
    c = core_mod.Core(cfg, store, Vault(store, b"k" * KEY_BYTES))
    c.live_configs = lambda: [{"key": "int:1", "name": "Gateway", "family": "sample", "state": st} for st in states()]
    rep = Reports(c, Path(tmp_path), clock=clock)
    rep.cert_check = lambda: []
    c.reports = rep
    store.set_setting("report_cfg", {"time": "06:30", "tz": TZ, "keep_days": 30, "link": ""})
    return c, rep


def section(report, sid):
    return next((s for s in report["sections"] if s["id"] == sid), None)


def texts(report, sec):
    out = {}
    for lang in languages():
        r = render.Renderer(lang, Z, report["day"])
        lines = [r.tr(sec["summary"]["key"], **r.values(sec["summary"]["vars"])), r.tr(sec["title"])] + [r.line(i) for i in sec["items"]]
        for line in lines:
            assert not re.search(r"\{|\}|report\.", line), (lang, line)
        out[lang] = lines
    return out


def test_section_only_with_zones_and_rules(tmp_path):
    clock = Clock(at(2026, 11, 18, 6, 30))
    c, rep = make(tmp_path, clock, lambda: [])
    r = asyncio.run(rep.run_due(clock.t))
    assert section(r, "zonematrix") is None
    no_rules = dict(STATE, policies=[])
    c.live_configs = lambda: [{"key": "int:1", "name": "Gateway", "state": no_rules}]
    clock.t = at(2026, 11, 19, 6, 30)
    r = asyncio.run(rep.run_due(clock.t))
    assert section(r, "zonematrix") is None


def test_baseline_then_opened_closed_and_internet(tmp_path):
    cur = {"st": copy.deepcopy(STATE)}
    clock = Clock(at(2026, 11, 18, 6, 30))
    c, rep = make(tmp_path, clock, lambda: [cur["st"]])
    r1 = asyncio.run(rep.run_due(clock.t))
    s1 = section(r1, "zonematrix")
    assert s1["summary"]["key"] == "report.zm.baseline" and s1["summary"]["vars"]["n"] == 2 and not s1["items"]
    t1 = texts(r1, s1)
    assert "2 allowed zone pairs" in t1["en"][0] and "2 erlaubte Zonenpaare" in t1["de"][0]
    assert "keep" not in (rep.get(r1["day"]) or {})                     # the comparison state is never shown

    # next night: IoT may reach Internal now, Internal no longer the internet, and the internet reaches IoT
    st = copy.deepcopy(STATE)
    st["policies"] = [pol("int-out", "z_int", "z_ext", enabled=False), pol("iot-out", "z_iot", "z_ext", order=10001),
                      pol("iot-int", "z_iot", "z_int", order=10002), pol("ext-iot", "z_ext", "z_iot", order=10003)]
    cur["st"] = st
    clock.t = at(2026, 11, 19, 6, 30)
    r2 = asyncio.run(rep.run_due(clock.t))
    s2 = section(r2, "zonematrix")
    keys = [i["key"] for i in s2["items"]]
    assert keys.count("report.zm.opened") == 2 and keys.count("report.zm.closed") == 1 and keys.count("report.zm.inbound") == 1
    assert s2["level"] == "danger" and s2["summary"]["key"] == "report.zm.summary_bad"
    assert s2["summary"]["vars"] == {"n": 2, "bad": 1}
    assert sum(1 for i in s2["items"] if i["point"]) == 3            # opened twice + inbound; a closed pair is a note
    t2 = texts(r2, s2)
    assert any("IoT → Internal is allowed now" in x for x in t2["en"])
    assert any("Internal → External ist jetzt gesperrt" in x for x in t2["de"])
    assert any("whole zone IoT takes traffic from the internet" in x for x in t2["en"])
    assert t2["en"][0] == "2 zone pairs opened, 1 zone open to the internet."
    assert t2["de"][0] == "2 Zonenpaare gingen auf, 1 Zone offen zum Internet."

    # third night, nothing changed: calm, but the open zone stays a point (same id, so a tick mark sticks)
    clock.t = at(2026, 11, 20, 6, 30)
    r3 = asyncio.run(rep.run_due(clock.t))
    s3 = section(r3, "zonematrix")
    assert [i["key"] for i in s3["items"]] == ["report.zm.inbound"]
    assert s3["items"][0]["id"] == next(i["id"] for i in s2["items"] if i["key"] == "report.zm.inbound")
    st["policies"] = [p for p in st["policies"] if p["id"] != "ext-iot"]
    clock.t = at(2026, 11, 21, 6, 30)
    r4 = asyncio.run(rep.run_due(clock.t))
    s4 = section(r4, "zonematrix")
    assert [i["key"] for i in s4["items"]] == ["report.zm.closed"]
    assert texts(r4, s4)
    clock.t = at(2026, 11, 22, 6, 30)
    r5 = asyncio.run(rep.run_due(clock.t))
    s5 = section(r5, "zonematrix")
    assert s5["summary"]["key"] == "report.zm.calm" and not s5["items"]
    t5 = texts(r5, s5)
    assert t5["en"][0] == "No zone pair opened or closed (2 allowed zone pairs)."
