"""Lanloom - a read-only circuit-board map of your local network."""
from pathlib import Path


def _read_version() -> str:
    for candidate in (Path(__file__).resolve().parent.parent / "VERSION", Path(__file__).resolve().parent / "VERSION"):
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return "0.0.0"


__version__ = _read_version()
