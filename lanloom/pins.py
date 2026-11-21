"""The image holds exactly the pinned package list.

The Dockerfile installs requirements.txt with ``pip install --no-deps`` followed by ``pip check``: a transitive package
without a pin breaks the build instead of being fetched unpinned behind the back of the dependency scan
(scripts/dep_scan.py scans the list, not the image). This module is the check from the other side, run by the
self-test inside the image (marker ``LANLOOM_IMAGE=1``, set by the Dockerfile): every installed distribution is on the
list with exactly its pinned version, nothing on the list is missing, nothing is installed in addition.

Outside the image (a development checkout, the test runner) extra packages such as the test tools are normal, so only
the "missing" and "wrong version" directions are meaningful there (see tests/test_pins.py)."""
import os
import re
from importlib import metadata
from pathlib import Path
from typing import Optional

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"
IMAGE_MARKER = "LANLOOM_IMAGE"
_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9._+!-]+)\s*$")


class PinError(ValueError):
    pass


def norm(name: str) -> str:
    """Normalised distribution name (PEP 503): case and runs of - _ . do not matter."""
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def pinned(path: Optional[Path] = None) -> dict:
    """{normalised name: version} of requirements.txt; every line must be pinned with == and appear once."""
    out = {}
    for raw in Path(path or REQUIREMENTS).read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _LINE.match(line)
        if not m:
            raise PinError("requirement is not pinned with ==: %s" % line)
        key = norm(m.group(1))
        if key in out:
            raise PinError("requirement listed twice: %s" % m.group(1))
        out[key] = m.group(2)
    if not out:
        raise PinError("no requirements found")
    return out


def installed() -> dict:
    """{normalised name: version} of every distribution Python can see."""
    out = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name") if dist.metadata else None
        if name:
            out.setdefault(norm(name), dist.version)
    return out


def compare(want: dict, have: dict) -> dict:
    """Differences between the pinned list and what is installed."""
    return {
        "missing": sorted(k for k in want if k not in have),
        "wrong": sorted("%s %s (pinned %s)" % (k, have[k], want[k]) for k in want if k in have and have[k] != want[k]),
        "extra": sorted("%s %s" % (k, have[k]) for k in have if k not in want),
    }


def problems(want: Optional[dict] = None, have: Optional[dict] = None) -> list:
    """Every difference as one line (empty = the image holds exactly the list)."""
    try:
        diff = compare(pinned() if want is None else want, installed() if have is None else have)
    except (OSError, PinError) as exc:
        return ["package list unreadable: %s" % exc]
    out = ["pinned package missing: %s" % k for k in diff["missing"]]
    out += ["installed version differs: %s" % k for k in diff["wrong"]]
    out += ["package installed without a pin: %s" % k for k in diff["extra"]]
    return out


def in_image() -> bool:
    return os.environ.get(IMAGE_MARKER) == "1"
