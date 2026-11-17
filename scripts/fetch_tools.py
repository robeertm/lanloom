#!/usr/bin/env python3
"""Downloads the pinned supply-chain tools (security/tools.json), checks the SHA-256 and unpacks them.

  fetch_tools.py --dest DIR [syft] [grype] [cosign]       (no names = all)
A tool that is already in DIR with the pinned version is kept (the file next to it records the checked hash).
Exit 0 = all present and checked, 1 = a download failed or a hash did not match (nothing is installed then).
Standard library only; the download goes to the release page of the tool's own project."""
import argparse
import hashlib
import io
import json
import os
import platform
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def pins() -> dict:
    return json.loads((ROOT / "security" / "tools.json").read_text())["tools"]


def fetch(name: str, dest: Path, arch: str = None) -> Path:
    spec = pins()[name]
    arch = arch or ARCH.get(platform.machine().lower())
    if arch not in spec["sha256"] or not sys.platform.startswith("lin"):
        raise RuntimeError("%s: no pinned file for %s" % (name, platform.machine()))
    want = spec["sha256"][arch]
    target = dest / name
    stamp = dest / (".%s.sha256" % name)
    if target.is_file() and stamp.is_file() and stamp.read_text().strip() == want:
        return target
    fname = (spec.get("archive") or spec["file"]).format(arch=arch, os=sys.platform)
    url = "https://github.com/%s/releases/download/v%s/%s" % (spec["repo"], spec["version"], fname)
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read(200 * 1024 * 1024)
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise RuntimeError("%s: SHA-256 mismatch for %s (got %s, pinned %s)" % (name, fname, got, want))
    if spec.get("archive"):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            member = tar.getmember(spec["member"])
            if not member.isfile():
                raise RuntimeError("%s: %s is not a regular file in the archive" % (name, spec["member"]))
            data = tar.extractfile(member).read()
    dest.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest, prefix=".%s." % name)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, target)
    stamp.write_text(want + "\n")
    return target


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", type=Path, required=True)
    ap.add_argument("--arch", choices=sorted(set(ARCH.values())))
    ap.add_argument("names", nargs="*")
    a = ap.parse_args(argv)
    names = a.names or sorted(pins())
    try:
        for n in names:
            print("%s %s -> %s" % (n, pins()[n]["version"], fetch(n, a.dest, a.arch)))
    except (RuntimeError, OSError, KeyError, tarfile.TarError) as exc:
        print("fetch_tools: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
