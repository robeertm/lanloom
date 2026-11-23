"""Scan progress in the page: Settings > General > Network scan and the setup wizard share one meter (LL.scanMeter).

- the meter: phase position + done/total in percent, never backwards within one scan (mDNS and SSDP report interleaved),
  starts over for the next scan, 100 only when the scan is done, an unknown phase counts as 0;
- Settings: while a scan runs, a progress bar sits left of the button and the button cancels (DELETE /api/scan), afterwards
  it starts a scan again; a scan that already runs when the page opens is picked up; polling ends when the page is left;
- the texts exist in every language (the completeness test checks the keys, this one the placeholders);
- "Last scan" (GET /api/scan "last") is the last finished scan: a stopped or running scan does not replace it."""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "lanloom" / "web"
I18N = ROOT / "lanloom" / "i18n"


def _run(seqs):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(ROOT / "tests" / "js" / "scanmeter_test.js"), json.dumps(seqs)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _s(id_, phase, done=0, total=0, state="running"):
    return {"id": id_, "state": state, "phase": phase, "done": done, "total": total}


def test_meter_follows_phases_and_never_moves_backwards():
    res = _run([[
        _s(1, "neighbors"),                       # no total yet
        _s(1, "neighbors", 128, 256),
        _s(1, "mdns", 80, 100),
        _s(1, "ssdp", 0, 100),
        _s(1, "mdns", 10, 100),                   # interleaved report of the parallel listener
        _s(1, "ports", 0, 500),
        _s(1, "identify", 3, 3),
        _s(1, "identify", 3, 3, state="done"),
    ]])
    assert res["phases"] == ["neighbors", "mdns", "ssdp", "ports", "banners", "identify"]
    out = res["out"][0]
    assert out == [0, 8, 30, 33, 33, 50, 99, 100]
    assert out == sorted(out)


def test_meter_starts_over_for_the_next_scan_and_ignores_unknown_phases():
    out = _run([[_s(1, "banners", 1, 2), _s(2, "neighbors", 0, 10), _s(2, "nonsense", 5, 10), _s(2, "mdns", 200, 100)]])["out"][0]
    assert out[0] == 75
    assert out[1] == 0                            # a new scan id: the bar starts at the left again
    assert out[2] == 0                            # an unknown phase never pushes the bar
    assert out[3] == 33                           # done above total is capped at the end of the phase


def test_remaining_time_estimate():
    eta = _run([[_s(1, "neighbors")]])["eta"]
    assert eta[0] is None                         # nothing known yet
    assert eta[1] == 98                           # 17 % after 20 s: 20 * 83 / 17 s left
    assert eta[2] is None                         # done: nothing left


@pytest.mark.parametrize("lang", sorted(p.stem for p in I18N.glob("*.json") if "." not in p.stem))
def test_remaining_time_text(lang):
    assert "{time}" in json.loads((I18N / (lang + ".json")).read_text(encoding="utf-8"))["settings.scan.eta"]


def test_meters_are_independent():
    out = _run([[_s(1, "ports", 1, 1)], [_s(1, "neighbors", 0, 4)]])["out"]
    assert out == [[67], [0]]


def _settings_scan_block():
    src = (WEB / "js" / "views.js").read_text(encoding="utf-8")
    i = src.index("const scanMsg = h('div', { class: 'msg' });")
    return src, src[i:src.index("// integrations", i)]


def test_settings_scan_card_has_bar_and_cancel():
    src, block = _settings_scan_block()
    assert "scanMeter, scanBtn" in block                       # bar left of the button, one row
    assert "role: 'progressbar'" in block
    assert "method: 'DELETE'" in block                         # the running button cancels
    assert "scanWatch.start(scan.current)" in block            # a scan already running is picked up
    assert "scanWatch.stop()" in block                         # polling ends when the page is left
    w = src[src.index("function watchScan("):]
    w = w[:w.index("\n  }\n") + 4]
    assert "LL.scanMeter()" in w                               # same meter as the wizard, not a second phase list
    assert "'wizard.scan.cancel'" in w and "'settings.scan.now'" in w
    assert "settings.scan.progress_n" in w
    assert "SCAN_PHASES" not in src and "'neighbors', 'mdns'" not in src


def test_wizard_uses_the_shared_meter():
    src = (WEB / "js" / "wizard.js").read_text(encoding="utf-8")
    pv = src[src.index("function progressView("):src.index("// ------------------------------------------------------------------ 4 review")]
    assert "LL.scanMeter()" in pv and "PHASES.indexOf(s.phase) / PHASES.length" not in pv


def test_bar_hides_when_idle():
    css = (WEB / "css" / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.scan-run__bar\[hidden\]\s*\{\s*display:\s*none", css)
    assert re.search(r"\.scan-run__bar\s*\{[^}]*flex:\s*1 1 auto", css)


@pytest.mark.parametrize("lang", sorted(p.stem for p in I18N.glob("*.json") if "." not in p.stem))
def test_progress_text_names_every_number(lang):
    texts = json.loads((I18N / (lang + ".json")).read_text(encoding="utf-8"))
    val = texts["settings.scan.progress_n"]
    for ph in ("{phase}", "{done}", "{total}", "{found}", "{pct}"):
        assert ph in val, (lang, ph)


def test_last_scan_is_the_last_finished_one(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from lanloom import config as config_mod
    from lanloom.main import create_app
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    hdr = {"X-Lanloom": "1"}
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        code = (tmp_path / "setup-code.txt").read_text().strip()
        assert c.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"}, headers=hdr).status_code == 200
        cidr = c.get("/api/networks").json()["networks"][0]["cidr"]

        def run(cancel):
            assert c.post("/api/scan", json={"cidrs": [cidr]}, headers=hdr).status_code == 200
            if cancel:
                time.sleep(0.6)
                assert c.delete("/api/scan", headers=hdr).status_code == 200
            for _ in range(120):
                st = c.get("/api/scan").json()
                if st["current"]["state"] != "running":
                    return st
                time.sleep(0.25)
            raise AssertionError("scan did not end")

        assert c.get("/api/scan").json()["last"] is None
        done = run(False)
        assert done["current"]["state"] == "done" and done["last"]["status"] == "done" and done["last"]["hosts"] > 20
        stopped = run(True)
        assert stopped["current"]["state"] == "cancelled"
        assert stopped["last"]["id"] == done["last"]["id"] and stopped["last"]["hosts"] == done["last"]["hosts"]
