#!/usr/bin/env python3
"""Dependency scan: the pinned Python packages (OSV database) and the container image (operating system and Python
packages, with grype). Policy (security/vulnerability-policy.json):

  * critical  -> RED, unless an exception with a reason and a review date covers it (an expired one does not);
  * high      -> notice, listed with its reason: an exception's reason, else the reason class
                 ("no fixed package in the base distribution yet", "fix available: <version>");
  * medium, low, unknown -> counted only (unknown is shown as a notice, so nothing hides behind a missing score).

  dep_scan.py python [--requirements FILE]                 Python packages through the OSV API (no install)
  dep_scan.py image REF [--platform P ...] --tools DIR     image REF (registry:<ref>, docker:<ref>, sbom:<file>)
  dep_scan.py bait --tools DIR                             self-check: a known critical package must be RED
  common: [--json FILE] [--policy FILE] [--md FILE]
Exit 0 = no red finding, 1 = red finding, 2 = the scan itself failed (network, tool) - never counted as green.
Standard library only (grype is a pinned binary, see scripts/fetch_tools.py)."""
import argparse
import datetime
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "security" / "vulnerability-policy.json"
OSV = "https://api.osv.dev/v1"
LEVELS = ("critical", "high", "medium", "low", "negligible", "unknown")
BAIT_PACKAGE = ("PyYAML", "5.3.1")          # CVE-2020-14343 / GHSA-8q59-q68h-6hv4, critical (9.8)
BAIT_ID = "GHSA-8q59-q68h-6hv4"


class ScanError(Exception):
    pass


# ------------------------------------------------------------------ CVSS 3.x base score (FIRST specification)
_W = {"AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}, "AC": {"L": 0.77, "H": 0.44},
      "UI": {"N": 0.85, "R": 0.62}, "C": {"H": 0.56, "L": 0.22, "N": 0}, "I": {"H": 0.56, "L": 0.22, "N": 0},
      "A": {"H": 0.56, "L": 0.22, "N": 0}}


def _roundup(x: float) -> float:
    i = int(round(x * 100000))
    return i / 100000.0 if i % 10000 == 0 else (math.floor(i / 10000) + 1) / 10.0


def cvss3_score(vector: str):
    m = dict(p.split(":", 1) for p in vector.split("/")[1:] if ":" in p)
    try:
        changed = m["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}[m["PR"]]
        iss = 1 - (1 - _W["C"][m["C"]]) * (1 - _W["I"][m["I"]]) * (1 - _W["A"][m["A"]])
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
        expl = 8.22 * _W["AV"][m["AV"]] * _W["AC"][m["AC"]] * pr * _W["UI"][m["UI"]]
    except KeyError:
        return None
    if impact <= 0:
        return 0.0
    return _roundup(min(1.08 * (impact + expl), 10)) if changed else _roundup(min(impact + expl, 10))


def level_of_score(score) -> str:
    if score is None:
        return "unknown"
    return "critical" if score >= 9.0 else "high" if score >= 7.0 else "medium" if score >= 4.0 else "low" if score > 0 else "negligible"


# ------------------------------------------------------------------ OSV (Python packages)
def _http_json(url: str, body=None, tries: int = 3):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "lanloom-dep-scan"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read(20 * 1024 * 1024))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if attempt == tries - 1:
                raise ScanError("OSV request failed: %s" % exc)
            time.sleep(3 * (attempt + 1))


def read_requirements(path: Path) -> list:
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9._+!-]+)\s*(?:;.*)?$", line)
        if not m:
            raise ScanError("requirement is not pinned with ==: %s" % line)
        out.append((m.group(1), m.group(2)))
    return out


def osv_severity(vuln: dict, fetch) -> str:
    label = str((vuln.get("database_specific") or {}).get("severity") or "").lower()
    if label in ("critical", "high", "low"):
        return label
    if label in ("moderate", "medium"):
        return "medium"
    for s in vuln.get("severity") or []:
        if s.get("type") == "CVSS_V3" and str(s.get("score", "")).startswith("CVSS:3"):
            return level_of_score(cvss3_score(s["score"]))
    for alias in vuln.get("aliases") or []:
        if alias.startswith("GHSA-") and alias != vuln.get("id"):
            other = fetch(alias)
            if other:
                return osv_severity(dict(other, aliases=[]), fetch)
    return "unknown"


def osv_fixed(vuln: dict, name: str) -> list:
    out = []
    norm = lambda n: re.sub(r"[-_.]+", "-", n).lower()
    for aff in vuln.get("affected") or []:
        if norm((aff.get("package") or {}).get("name", "")) != norm(name):
            continue
        for rng in aff.get("ranges") or []:
            out += [e["fixed"] for e in rng.get("events") or [] if "fixed" in e]
    return sorted(set(out))


def scan_python(packages: list) -> list:
    cache = {}

    def fetch(vid):
        if vid not in cache:
            try:
                cache[vid] = _http_json("%s/vulns/%s" % (OSV, vid))
            except ScanError:
                cache[vid] = None
        return cache[vid]

    queries = [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in packages]
    results = []
    for i in range(0, len(queries), 500):
        part = _http_json(OSV + "/querybatch", {"queries": queries[i:i + 500]})
        results += part.get("results") or []
    if len(results) != len(packages):
        raise ScanError("OSV answered %d of %d queries" % (len(results), len(packages)))
    findings, seen = [], set()
    for (name, version), res in zip(packages, results):
        for v in res.get("vulns") or []:
            vuln = fetch(v["id"])
            if vuln is None:
                raise ScanError("OSV entry %s could not be read" % v["id"])
            ids = {vuln["id"], *(vuln.get("aliases") or [])}
            key = (name.lower(), frozenset(ids))
            if any(k[0] == key[0] and k[1] & key[1] for k in seen):   # the same flaw under another name
                continue
            seen.add(key)
            fixed = osv_fixed(vuln, name)
            findings.append({"id": vuln["id"], "aliases": sorted(ids - {vuln["id"]}), "package": name, "version": version,
                             "type": "python", "severity": osv_severity(vuln, fetch),
                             "fix_state": "fixed" if fixed else "unknown", "fixed_in": fixed, "platforms": ["requirements.txt"],
                             "source": "osv"})
    return findings


# ------------------------------------------------------------------ grype (image)
def grype_env(tools: Path) -> dict:
    env = dict(os.environ)
    env.setdefault("GRYPE_DB_CACHE_DIR", str(Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "lanloom-grype-db"))
    env["GRYPE_CHECK_FOR_APP_UPDATE"] = "false"
    env["SYFT_CHECK_FOR_APP_UPDATE"] = "false"
    return env


def run_grype(tools: Path, source: str, platform: str = None) -> dict:
    exe = tools / "grype"
    if not exe.is_file():
        raise ScanError("grype not found in %s (scripts/fetch_tools.py --dest %s grype)" % (tools, tools))
    cmd = [str(exe), source, "-o", "json", "-q"] + (["--platform", platform] if platform else [])
    p = subprocess.run(cmd, capture_output=True, env=grype_env(tools), timeout=3600)
    if p.returncode != 0:
        raise ScanError("grype failed (%d): %s" % (p.returncode, p.stderr.decode("utf-8", "replace")[-500:]))
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise ScanError("grype did not return JSON")


def from_grype(doc: dict, platform: str) -> list:
    out = []
    for m in doc.get("matches") or []:
        v, a = m.get("vulnerability") or {}, m.get("artifact") or {}
        fix = v.get("fix") or {}
        sev = str(v.get("severity") or "unknown").lower()
        out.append({"id": v.get("id", "?"), "aliases": sorted({r.get("id") for r in m.get("relatedVulnerabilities") or []} - {v.get("id"), None}),
                    "package": a.get("name", "?"), "version": a.get("version", "?"), "type": a.get("type", "?"),
                    "severity": sev if sev in LEVELS else "unknown", "fix_state": fix.get("state") or "unknown",
                    "fixed_in": list(fix.get("versions") or []), "platforms": [platform], "source": "grype"})
    return out


def merge(findings: list) -> list:
    """One row per (flaw, package, version); platforms are joined."""
    rows = {}
    for f in findings:
        key = (f["id"], f["package"], f["version"])
        if key in rows:
            rows[key]["platforms"] = sorted(set(rows[key]["platforms"]) | set(f["platforms"]))
        else:
            rows[key] = dict(f)
    return list(rows.values())


# ------------------------------------------------------------------ policy
def load_policy(path: Path) -> dict:
    data = json.loads(path.read_text())
    for e in data.get("exceptions") or []:
        if not str(e.get("reason", "")).strip() or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(e.get("review_by", ""))):
            raise ScanError("exception %s needs a reason and review_by (YYYY-MM-DD)" % e.get("id"))
    return data


def evaluate(findings: list, policy: dict, today: datetime.date = None) -> dict:
    today = today or datetime.date.today()
    red, notices, counted = [], [], {lvl: 0 for lvl in LEVELS}
    for f in sorted(findings, key=lambda f: (LEVELS.index(f["severity"]), f["package"], f["id"])):
        counted[f["severity"]] += 1
        ids = {f["id"], *f.get("aliases", [])}
        exc = next((e for e in policy.get("exceptions") or [] if e["id"] in ids
                    and (not e.get("packages") or f["package"] in e["packages"])), None)
        expired = exc is not None and datetime.date.fromisoformat(exc["review_by"]) < today
        if exc and not expired:
            reason = "exception: %s (review by %s)" % (exc["reason"], exc["review_by"])
        elif f["fix_state"] == "fixed" and f["fixed_in"]:
            reason = "fix available: %s" % ", ".join(f["fixed_in"][:3])
        elif f["fix_state"] in ("not-fixed", "wont-fix", "unknown") and f["type"] not in ("python",):
            reason = "no fixed package in the base distribution yet (status %s)" % f["fix_state"]
        else:
            reason = "no fixed version known yet"
        if expired:
            reason += "; the exception expired on %s" % exc["review_by"]
        row = dict(f, reason=reason, excepted=bool(exc and not expired))
        if f["severity"] == "critical" and not row["excepted"]:
            red.append(row)
        elif f["severity"] in ("critical", "high", "unknown"):
            notices.append(row)
    return {"red": red, "notices": notices, "counts": counted, "total": len(findings)}


def render(title: str, result: dict) -> str:
    lines = ["== %s: %d findings (%s)" % (title, result["total"],
             ", ".join("%s %d" % (k, v) for k, v in result["counts"].items() if v) or "none")]
    for row in result["red"]:
        lines.append("RED     %-8s %-22s %-28s %s [%s] - %s" % (row["severity"], row["id"], row["package"], row["version"],
                                                             "/".join(row["platforms"]), row["reason"]))
    for row in result["notices"]:
        lines.append("notice  %-8s %-22s %-28s %s [%s] - %s" % (row["severity"], row["id"], row["package"], row["version"],
                                                             "/".join(row["platforms"]), row["reason"]))
    lines.append("== %s: %s" % (title, "RED (%d critical without a valid exception)" % len(result["red"]) if result["red"] else "GREEN"))
    return "\n".join(lines)


def markdown(title: str, result: dict) -> str:
    out = ["### %s" % title, "", "%d findings: %s" % (result["total"], ", ".join("%s %d" % (k, v) for k, v in result["counts"].items() if v) or "none"), ""]
    rows = result["red"] + result["notices"]
    if rows:
        out += ["| | Severity | Id | Package | Version | Reason |", "|---|---|---|---|---|---|"]
        out += ["| %s | %s | %s | %s | %s | %s |" % ("RED" if r in result["red"] else "notice", r["severity"], r["id"], r["package"],
                                                   r["version"], r["reason"].replace("|", "/")) for r in rows]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ commands
def cmd_python(a) -> tuple:
    return "Python packages (%s)" % a.requirements.name, merge(scan_python(read_requirements(a.requirements)))


def cmd_image(a) -> tuple:
    found = []
    for p in a.platform or [None]:
        found += from_grype(run_grype(a.tools, a.ref, p), p or "default")
    return "Image %s" % a.ref, merge(found)


def cmd_bait(a) -> tuple:
    """A known critical package must come back RED from both sources; otherwise the scan is broken (exit 2)."""
    py = evaluate(scan_python([BAIT_PACKAGE]), {"exceptions": []})
    ok_osv = any(r["id"] == BAIT_ID or BAIT_ID in r["aliases"] for r in py["red"])
    ok_grype = True
    if a.tools:
        purl = Path(a.tools) / "bait.purl"
        purl.write_text("pkg:pypi/%s@%s\n" % (BAIT_PACKAGE[0].lower(), BAIT_PACKAGE[1]))
        gr = evaluate(from_grype(run_grype(a.tools, "purl:%s" % purl), "bait"), {"exceptions": []})
        purl.unlink()
        ok_grype = any(r["id"] == BAIT_ID or BAIT_ID in r["aliases"] for r in gr["red"])
    print("bait %s==%s: OSV %s, grype %s" % (BAIT_PACKAGE[0], BAIT_PACKAGE[1], "RED (as expected)" if ok_osv else "NOT FOUND",
                                             ("RED (as expected)" if ok_grype else "NOT FOUND") if a.tools else "skipped"))
    if not (ok_osv and ok_grype):
        raise ScanError("self-check failed: the known critical package was not reported")
    return None, None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("python", "image", "bait"):
        p = sub.add_parser(name)
        p.add_argument("--json", type=Path)
        p.add_argument("--md", type=Path, help="append a Markdown table (for a CI job summary)")
        p.add_argument("--policy", type=Path, default=POLICY)
        p.add_argument("--tools", type=Path, required=(name == "image"))
        if name == "python":
            p.add_argument("--requirements", type=Path, default=ROOT / "requirements.txt")
        if name == "image":
            p.add_argument("ref")
            p.add_argument("--platform", action="append")
    a = ap.parse_args(argv)
    try:
        title, findings = {"python": cmd_python, "image": cmd_image, "bait": cmd_bait}[a.cmd](a)
        if title is None:
            return 0
        result = evaluate(findings, load_policy(a.policy))
    except (ScanError, subprocess.TimeoutExpired, OSError) as exc:
        print("dep_scan: SCAN FAILED - %s" % exc, file=sys.stderr)
        return 2
    print(render(title, result))
    if a.json:
        a.json.write_text(json.dumps(dict(result, title=title), indent=1))
    if a.md:
        with open(a.md, "a") as f:
            f.write(markdown(title, result))
    return 1 if result["red"] else 0


if __name__ == "__main__":
    sys.exit(main())
