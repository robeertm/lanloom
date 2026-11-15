"""Checks for the generated offline world base map (no network)."""
import re
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "lanloom" / "web" / "js" / "worldmap.js"


def test_worldmap_generated_file():
    assert FILE.is_file()
    raw = FILE.read_bytes()
    assert len(raw) < 80 * 1024
    text = raw.decode("utf-8")
    assert "GENERATED" in text
    assert '"DE":{"d":"M' in text
    assert '"US":' in text
    assert "Lanloom.worldmap" in text
    body = text.split("*/", 1)[1]
    assert not re.search(r"[^\x00-\x7f]", body)
