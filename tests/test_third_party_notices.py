"""THIRD-PARTY-NOTICES.md names every pinned package, the bundled font and every data file (scripts/drittlizenzen.py)."""
import importlib.util
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("drittlizenzen", ROOT / "scripts" / "drittlizenzen.py")
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)


def test_notices_are_complete():
    assert dl.problems() == []


def test_a_missing_dependency_turns_the_check_red():
    text = (ROOT / dl.NOTICES).read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if l.startswith("| cryptography |"))
    probs = dl.problems(text=text.replace(line + "\n", ""))
    assert any("cryptography" in p for p in probs)


def test_a_new_pin_without_a_row_is_reported(tmp_path):
    for rel in (dl.NOTICES, "requirements.txt"):
        shutil.copy(ROOT / rel, tmp_path / rel)
    shutil.copytree(ROOT / "lanloom/web/fonts", tmp_path / "lanloom/web/fonts")
    shutil.copytree(ROOT / "lanloom/data", tmp_path / "lanloom/data")
    with open(tmp_path / "requirements.txt", "a", encoding="utf-8") as f:
        f.write("brandnewpkg==1.0.0\n")
    assert any("brandnewpkg" in p for p in dl.problems(tmp_path))
    assert dl.write(tmp_path) == 1  # licence unknown: stops instead of guessing


def test_write_keeps_a_complete_file_unchanged(tmp_path):
    for rel in (dl.NOTICES, "requirements.txt"):
        shutil.copy(ROOT / rel, tmp_path / rel)
    before = (tmp_path / dl.NOTICES).read_text(encoding="utf-8")
    assert dl.write(tmp_path) == 0
    assert (tmp_path / dl.NOTICES).read_text(encoding="utf-8") == before


def test_a_data_file_without_notice_is_reported(tmp_path):
    shutil.copytree(ROOT / "lanloom/web/fonts", tmp_path / "lanloom/web/fonts")
    shutil.copytree(ROOT / "lanloom/data", tmp_path / "lanloom/data")
    shutil.copy(ROOT / "requirements.txt", tmp_path / "requirements.txt")
    (tmp_path / "lanloom/data/mystery.bin").write_bytes(b"x")
    text = (ROOT / dl.NOTICES).read_text(encoding="utf-8")
    assert any("mystery.bin" in p for p in dl.problems(tmp_path, text))
