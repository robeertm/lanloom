"""O14: one frame gate for the data path maps and the energy figure - a ceiling by the motion setting, whatever the screen speed."""
from pathlib import Path

from _board_node import run_node

WEB = Path(__file__).resolve().parents[1] / "lanloom" / "web"


def test_frame_ceiling_by_motion_setting_at_any_screen_speed():
    out = run_node("frametick_test.js")
    for gap, per in out["gaps"].items():          # 6 ms (166 Hz), 120 Hz, 60 Hz
        assert per["auto"] <= 30.5 and per["auto"] >= 25, (gap, per)
        assert per["full"] <= 60.5 and per["full"] >= 45, (gap, per)
        assert per["saver"] <= 15.5 and per["saver"] >= 12, (gap, per)
        assert per["off"] == 0
    assert out["autoStart"] == 30


def test_self_throttling_stays_below_the_ceiling():
    out = run_node("frametick_test.js")
    assert out["slow"] < 60 and out["recovered"] == 60


def test_all_four_loops_use_the_gate_and_the_clock():
    assert 'src="js/frametick.js"' in (WEB / "index.html").read_text(encoding="utf-8")
    for name in ("paths.js", "paths_cables.js", "energy.js"):
        js = (WEB / "js" / name).read_text(encoding="utf-8")
        assert "LL.frameGate()" in js and "gate.draw(ts)" in js, name
        assert "anim.level" not in js and "calmSince" not in js, name   # the old per-file self-throttling is gone
    # paths_country.js draws through runDots of paths_cables.js
    assert "runDots" in (WEB / "js" / "paths_country.js").read_text(encoding="utf-8")
    # movement by timestamp, not by frame count
    assert "ts / p.dur" in (WEB / "js" / "paths.js").read_text(encoding="utf-8")
    assert "ts / p.dur" in (WEB / "js" / "paths_cables.js").read_text(encoding="utf-8")
    assert "dt * PACE" in (WEB / "js" / "energy.js").read_text(encoding="utf-8")
