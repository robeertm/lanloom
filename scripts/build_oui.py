#!/usr/bin/env python3
"""Build lanloom/data/oui.bin.gz from the public IEEE registry lists (MA-L, MA-M, MA-S).

usage:  python3 -I scripts/build_oui.py oui.csv mam.csv oui36.csv [--date YYYY-MM-DD]

Download the three CSV files yourself from https://standards-oui.ieee.org/ (oui/oui.csv, oui28/mam.csv,
oui36/oui36.csv). They are untrusted input: only parsed as CSV text, never executed. The script checks the input
before it writes anything (header, registry type per file, hexadecimal assignments of the right length, enough
rows), then writes the compact file next to the old one and replaces it atomically. Lanloom never downloads this
file at run time; a new table arrives with a new release.
"""
import csv
import datetime
import gzip
import io
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lanloom import oui  # noqa: E402

KINDS = {"MA-L": (24, 6), "MA-M": (28, 7), "MA-S": (36, 9)}
MIN_ROWS = {"MA-L": 30_000, "MA-M": 4_000, "MA-S": 4_000}
SKIP_NAMES = {"ieee registration authority", "private"}


def read(path: Path):
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit("%s unreadable: %s" % (path.name, type(exc).__name__))
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or [c.strip() for c in rows[0][:3]] != ["Registry", "Assignment", "Organization Name"]:
        raise SystemExit("%s: unexpected header" % path.name)
    out = []
    kinds = set()
    for n, row in enumerate(rows[1:], 2):
        if not row:
            continue
        if len(row) < 3:
            raise SystemExit("%s line %d: too few columns" % (path.name, n))
        kind, assign, name = row[0].strip(), row[1].strip().upper(), row[2]
        if kind not in KINDS:
            raise SystemExit("%s line %d: unknown registry %r" % (path.name, n, kind[:10]))
        bits, digits = KINDS[kind]
        if not re.fullmatch(r"[0-9A-F]{%d}" % digits, assign):
            raise SystemExit("%s line %d: bad assignment" % (path.name, n))
        kinds.add(kind)
        out.append((kind, bits, int(assign, 16), name))
    if len(kinds) != 1:
        raise SystemExit("%s: mixes registries" % path.name)
    kind = kinds.pop()
    if len(out) < MIN_ROWS[kind]:
        raise SystemExit("%s: only %d rows" % (path.name, len(out)))
    return out


def build(paths, date: str) -> bytes:
    names, index = [""], {"": 0}
    tables = {24: {}, 28: {}, 36: {}}
    for p in paths:
        for _kind, bits, prefix, raw_name in read(Path(p)):
            name = oui.clean_name(raw_name)
            if name.lower() in SKIP_NAMES or raw_name.strip().lower() in SKIP_NAMES:
                name = ""
            if name and name not in index:
                index[name] = len(names)
                names.append(name)
            tables[bits][prefix] = index[name]
    if not all(tables.values()):
        raise SystemExit("all three lists are needed (MA-L, MA-M, MA-S)")
    raw = oui.pack({"date": date, "source": oui.SOURCE["text"]}, names, {b: list(t.items()) for b, t in tables.items()})
    oui.OuiDB(raw)                       # the result must load
    return raw


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    date = datetime.date.today().isoformat()
    if "--date" in argv:
        date = argv[argv.index("--date") + 1]
        args.remove(date)
    if len(args) != 3:
        print(__doc__)
        return 2
    raw = build(args, date)
    target = ROOT / "lanloom" / "data" / "oui.bin.gz"
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
    db = oui.OuiDB.from_file(tmp)
    os.replace(tmp, target)
    print("wrote %s: %d prefixes, %d names, %d bytes" % (target.relative_to(ROOT), db.rows, len(db.names), target.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
