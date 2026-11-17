#!/usr/bin/env python3
"""Secret scanner over the repository (the repository is public).

Looks for the shape of real secrets, not for known values: private keys, hosted-git and cloud tokens, chat and
bot tokens, signed web tokens, passwords inside URLs, and assignments of long random values to names like
password/secret/token/api_key. Every tracked text file is read (git ls-files); with --history every added line of
every commit is read as well. A line that holds a deliberate fake (a test that such input is refused) carries the
marker comment "secret-scan: fake"; such lines that are already in the history without the marker are listed by
their SHA-256 in security/secret-scan-baseline.json. Values are never printed, only file, line and kind.

  secret_scan.py [--root DIR] [--history]
Exit 0 = nothing found, 1 = findings, 2 = git not usable.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = "secret-scan: fake"
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".gz", ".bin", ".zip", ".pdf", ".mp4"}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
    "hosted git token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,})"),
    "cloud access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "cloud api key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "chat token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "bot token": re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b"),
    "ai service key": re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}"),
    "payment key": re.compile(r"\b[sr]k_live_[A-Za-z0-9]{20,}"),
    "signed web token": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "password in URL": re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://[^\s/:@'\"<>{}$]+:[^\s/@'\"<>{}$*]{6,}@[A-Za-z0-9]"),
    "secret assignment": re.compile(
        r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|client[_-]?secret)\b[\"']?\s*[:=]\s*[\"']"
        r"(?=[A-Za-z0-9+/_-]{24,}[\"'])(?=[^\"']*\d)(?=[^\"']*[A-Za-z])[A-Za-z0-9+/_-]{24,}[\"']"),
}
# values that look random but are not secrets: placeholders and test values the code uses on purpose
PLACEHOLDER = re.compile(r"(?i)(example|placeholder|dummy|changeme|your[_-]|xxxx|0123456789|abcdef|test|fake|sample|:password@|:pass@)")
BASELINE = "security/secret-scan-baseline.json"


def load_baseline(root: Path) -> set:
    """SHA-256 of reviewed lines in the history that hold a deliberate fake (the value itself is never stored)."""
    try:
        return {e["sha256"] for e in json.loads((root / BASELINE).read_text(encoding="utf-8")).get("reviewed", [])}
    except (OSError, ValueError, KeyError):
        return set()


def line_hash(line: str) -> str:
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()


def scan_text(text: str, where: str, baseline: frozenset = frozenset()) -> list:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if MARKER in line or (baseline and line_hash(line) in baseline):
            continue
        for kind, rx in PATTERNS.items():
            m = rx.search(line)
            if m and not (kind in ("secret assignment", "password in URL") and PLACEHOLDER.search(m.group(0))):
                out.append((where, n, kind))
    return out


def tracked_files(root: Path) -> list:
    p = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True)
    if p.returncode:
        raise RuntimeError("git ls-files failed")
    return [root / f for f in p.stdout.decode().split("\0") if f]


def scan_tree(root: Path) -> list:
    out, baseline = [], frozenset(load_baseline(root))
    for path in tracked_files(root):
        if path.suffix.lower() in SKIP_SUFFIX or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out += scan_text(text, path.relative_to(root).as_posix(), baseline)
    return out


def scan_history(root: Path) -> list:
    """Added lines of every commit (a secret that was removed later is still in the history)."""
    p = subprocess.run(["git", "-C", str(root), "log", "--all", "-p", "--no-color", "--no-ext-diff", "-U0",
                        "--format=commit %H"], capture_output=True)
    if p.returncode:
        raise RuntimeError("git log failed")
    out, commit, fname, skip = [], "?", "?", False
    baseline = frozenset(load_baseline(root))
    for raw in p.stdout.decode("utf-8", "replace").splitlines():
        if raw.startswith("commit "):
            commit = raw[7:15]
        elif raw.startswith("+++ "):
            fname = raw[6:] if raw.startswith("+++ b/") else raw[4:]
            skip = Path(fname).suffix.lower() in SKIP_SUFFIX
        elif raw.startswith("+") and not skip:
            for _, _, kind in scan_text(raw[1:], fname, baseline):
                out.append(("%s@%s" % (fname, commit), 0, kind))
    return sorted(set(out))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--history", action="store_true", help="also read every added line of the git history")
    a = ap.parse_args(argv)
    try:
        found = scan_tree(a.root) + (scan_history(a.root) if a.history else [])
    except RuntimeError as exc:
        print("secret scan: %s" % exc, file=sys.stderr)
        return 2
    for where, line, kind in found:
        print("SECRET? %s%s: %s" % (where, ":%d" % line if line else "", kind))
    if found:
        print("-> remove it (and rotate it if it was real); a deliberate fake gets the comment '%s'." % MARKER)
        return 1
    print("secret scan: nothing found (%s)" % ("files and history" if a.history else "tracked files"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
