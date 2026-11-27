#!/usr/bin/env python3
"""Keeps THIRD-PARTY-NOTICES.md in step with what Lanloom ships.

  python3 -I scripts/drittlizenzen.py --check    exit 1 and name what is missing or stale (default)
  python3 -I scripts/drittlizenzen.py --write    rewrite the "Python packages" table from requirements.txt

Sources: requirements.txt (every pinned package, exact version), the bundled font under lanloom/web/fonts and the
data files under lanloom/data. For a package that is new, the licence is read from the installed package metadata
when it is installed in the running environment; otherwise --write stops and asks for the row to be filled in by
hand. Existing rows keep their licence text. tests/test_third_party_notices.py runs the check, so a dependency
without a row turns the tests red.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTICES = "THIRD-PARTY-NOTICES.md"
ROW = re.compile(r"^\| ([A-Za-z0-9._-]+) \| ([0-9][^ |]*) \| (.+?) \|$", re.M)
# Data files that are not covered by their own row of the notices (generated indexes of a listed source).
DATA_COVERED_BY_NAME = {"cables-open.sha256", "cables-osm.sha256", "CABLES-LICENSE.md", "eol.json"}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pinned(root: Path = ROOT) -> dict:
    out = {}
    for line in (root / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==([0-9][^\s;]*)", line)
        if m:
            out[norm(m.group(1))] = m.group(2)
    return out


def listed(text: str) -> dict:
    block = text.split("## Python packages", 1)[1].split("\n## ", 1)[0]
    return {norm(n): (v, lic) for n, v, lic in ROW.findall(block)}


def problems(root: Path = ROOT, text: str | None = None) -> list:
    text = text if text is not None else (root / NOTICES).read_text(encoding="utf-8")
    want, have = pinned(root), listed(text)
    out = [f"missing in {NOTICES}: {n}=={v}" for n, v in sorted(want.items()) if n not in have]
    out += [f"not pinned any more: {n}" for n in sorted(have) if n not in want]
    out += [f"version differs: {n} pinned {want[n]}, listed {have[n][0]}"
            for n in sorted(want) if n in have and have[n][0] != want[n]]
    out += [f"empty licence: {n}" for n, (_, lic) in sorted(have.items()) if not lic.strip()]
    for f in sorted((root / "lanloom/web/fonts").glob("*.woff2")):
        if f.name not in text:
            out.append(f"font without notice: {f.name}")
    for f in sorted((root / "lanloom/data").iterdir()):
        if f.name not in DATA_COVERED_BY_NAME and f.name not in text:
            out.append(f"data file without notice: {f.name}")
    return out


def _metadata_row(name: str, version: str) -> str | None:
    try:
        import importlib.metadata as md
        m = md.metadata(name)
    except Exception:
        return None
    lic = (m.get("License-Expression") or "").strip()
    if not lic:
        lic = next((c.split("::")[-1].strip() for c in m.get_all("Classifier", []) if c.startswith("License ::")), "")
    return f"| {name} | {version} | {lic} |" if lic else None


def write(root: Path = ROOT) -> int:
    path = root / NOTICES
    text = path.read_text(encoding="utf-8")
    old, want = listed(text), pinned(root)
    rows, unknown = [], []
    names = {norm(n): n for n, _, _ in ROW.findall(text)}
    for key in [k for k in old if k in want] + [k for k in want if k not in old]:
        ver = want[key]
        if key in old:
            rows.append(f"| {names[key]} | {ver} | {old[key][1]} |")
        else:
            r = _metadata_row(key, ver)
            (rows.append(r) if r else unknown.append(key))
    if unknown:
        print("licence unknown, add a row by hand: " + ", ".join(unknown), file=sys.stderr)
        return 1
    head, rest = text.split("## Python packages", 1)
    block, tail = (rest.split("\n## ", 1) + [""])[:2]
    note = block[block.index("\n\nWhere a package"):] if "\n\nWhere a package" in block else "\n"
    new = head + "## Python packages\n\n| Package | Version | License |\n| --- | --- | --- |\n" + "\n".join(rows) + note
    new = new.rstrip("\n") + "\n" + ("\n## " + tail if tail else "")
    path.write_text(new, encoding="utf-8")
    return 0


def main(argv: list) -> int:
    if "--write" in argv:
        return write()
    probs = problems()
    for p in probs:
        print(p, file=sys.stderr)
    print("notices ok" if not probs else f"{len(probs)} problem(s)")
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
