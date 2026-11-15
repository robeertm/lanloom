#!/usr/bin/env python3
"""Build lanloom/data/geo-country.bin.gz from the free DB-IP "IP to Country Lite" CSV (CC BY 4.0).

usage:  python3 -I scripts/build_geodb.py dbip-country-lite-YYYY-MM.csv.gz

Download the CSV yourself from https://db-ip.com/db/download/ip-to-country-lite (the file is untrusted input:
it is only parsed as text, never executed). The script checks the input before it writes anything:
complete gzip, every row "start,end,CC" with addresses of one family, ascending and without gaps, enough rows,
and well-known samples. Then it writes the compact file next to the old one and replaces it atomically.
Lanloom never downloads this file at run time; a new month arrives with a new release.
"""
import gzip
import ipaddress
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lanloom import geo  # noqa: E402

MIN_ROWS = {4: 200_000, 6: 100_000}
NAME_RX = re.compile(r"dbip-country-lite-(\d{4}-\d{2})\.csv\.gz$")


def read(path: Path):
    try:
        text = gzip.decompress(path.read_bytes()).decode("utf-8")
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise SystemExit("input unreadable: %s" % type(exc).__name__)
    codes = [geo.RESERVED]
    index = {geo.RESERVED: 0}
    fam = {4: ([], [], None), 6: ([], [], None)}
    last_end = {4: -1, 6: -1}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split(",")
        if len(parts) != 3:
            raise SystemExit("row %d: expected start,end,code" % n)
        try:
            a, b = ipaddress.ip_address(parts[0].strip()), ipaddress.ip_address(parts[1].strip())
        except ValueError:
            raise SystemExit("row %d: bad address" % n)
        cc = parts[2].strip().strip('"').upper()
        if a.version != b.version or int(a) > int(b) or not re.fullmatch(r"[A-Z]{2}", cc):
            raise SystemExit("row %d: bad range or code" % n)
        v = a.version
        if int(a) != last_end[v] + 1:
            raise SystemExit("row %d: not ascending without gaps" % n)
        last_end[v] = int(b)
        if cc not in index:
            index[cc] = len(codes)
            codes.append(cc)
            if len(codes) > 255:
                raise SystemExit("too many country codes")
        starts, idx, _ = fam[v]
        key = int(a) if v == 4 else int(a) >> 64
        i = index[cc]
        if idx and idx[-1] == i:
            continue                       # same country as the range before: the earlier start covers it
        if starts and starts[-1] == key:   # IPv6 ranges finer than /64 collapse: the later range wins
            idx[-1] = i
            if len(idx) > 1 and idx[-2] == i:
                starts.pop(); idx.pop()
            continue
        starts.append(key)
        idx.append(i)
    for v in (4, 6):
        if len(fam[v][0]) < MIN_ROWS[v] // 3:
            raise SystemExit("too few IPv%d rows (%d)" % (v, len(fam[v][0])))
    if last_end[4] != 2 ** 32 - 1 or last_end[6] != 2 ** 128 - 1:
        raise SystemExit("ranges do not reach the end of the address space")
    return codes, fam


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    src = Path(argv[1])
    m = NAME_RX.search(src.name)
    month = m.group(1) if m else ""
    codes, fam = read(src)
    raw = geo.pack({"source": "DB-IP IP to Country Lite", "license": "CC BY 4.0", "month": month, "codes": codes},
                   fam[4][0], fam[4][1], fam[6][0], fam[6][1])
    db = geo.GeoDB(raw)
    try:
        db.check_samples()
    except geo.GeoError as exc:
        raise SystemExit(str(exc))
    out = ROOT / "lanloom" / "data" / "geo-country.bin.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
    geo.GeoDB.from_file(tmp)
    os.replace(tmp, out)
    print("wrote %s: month %s, %d IPv4 + %d IPv6 starts, %d codes, %d bytes"
          % (out.relative_to(ROOT), month or "?", len(fam[4][0]), len(fam[6][0]), len(codes), out.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
