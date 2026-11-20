#!/usr/bin/env python3
"""Stamp the landing page with the current version: the social preview image is addressed as social.png?v=<VERSION>,
because link previews are kept per address - a new version means a new address and so a new picture.

  site_stamp.py [--check] [--root DIR]
Without --check the image address in docs/site/index.html are rewritten; with --check nothing is written and the
exit code is 1 when a stamp differs from the VERSION file (the Pages workflow stamps before it publishes)."""
import argparse
import re
import sys
from pathlib import Path

PATTERN = re.compile(r"(social\.png\?v=)([0-9A-Za-z.\-]+)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    a = ap.parse_args(argv)
    root = Path(a.root)
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    page = root / "docs" / "site" / "index.html"
    text = page.read_text(encoding="utf-8")
    found = PATTERN.findall(text)
    if not found:
        print("site_stamp: expected the og:image address with ?v=", file=sys.stderr)
        return 1
    new = PATTERN.sub(lambda m: m.group(1) + version, text)
    if a.check:
        stale = [v for _, v in found if v != version]
        if stale:
            print("site_stamp: the page carries %s, VERSION is %s" % (sorted(set(stale)), version), file=sys.stderr)
            return 1
        return 0
    if new != text:
        page.write_text(new, encoding="utf-8")
        print("site_stamp: stamped %s" % version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
