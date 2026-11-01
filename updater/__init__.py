"""Lanloom update sidecar.

Runs from the same image as the app (`python -m updater`), as root, in its own
container with the container runtime socket mounted. It never listens on the
network: the app asks for an update by writing a small request file on the
shared data volume and the sidecar reports progress in a status file.
"""

from __future__ import annotations

from pathlib import Path


def sidecar_version() -> str:
    """Version of the image the sidecar runs from (same VERSION as the app)."""
    try:
        from lanloom import __version__  # type: ignore

        if isinstance(__version__, str) and __version__:
            return __version__
    except Exception:
        pass
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip() or "0.0.0"
    except OSError:
        return "0.0.0"
