#!/usr/bin/env python3
"""Dependency scan: the pinned Python packages (OSV database) and the container image (operating system and Python
packages, with grype). Policy (security/vulnerability-policy.json):

  * critical, high -> RED, unless a valid exception covers the finding: advisory id, the packages it applies to, the
                     kind ("not-reachable" - the flaw does not reach Lanloom, or "must-wait" - no fixed package yet),
                     a reason that says why, the date it was checked and a review date at most 183 days later.
                     An expired exception counts as missing. A covered finding stays visible as a notice with its reason.
  * unknown       -> notice (nothing hides behind a missing score);
  * medium, low   -> counted only.
The policy file states these rules too; a policy that would make critical or high anything but red is refused (exit 2),
so the rule cannot be weakened by editing the file alone.

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
BAIT_HIGH_PACKAGE = ("urllib3", "1.26.4")   # CVE-2021-33503 / GHSA-q2q7-5pp4-w6pg, high (7.5), no critical advisory
BAIT_HIGH_ID = "GHSA-q2q7-5pp4-w6pg"
RED_LEVELS = ("critical", "high")          # always red without a valid exception (same rule as the shared release gate)
RULE_VALUES = ("red", "notice", "count")
DEFAULT_RULES = {"critical": "red", "high": "red", "medium": "count", "low": "count", "negligible": "count", "unknown": "notice"}
EXCEPTION_KINDS = ("not-reachable", "must-wait")
EXCEPTION_MAX_DAYS = 183                    # review_by at most this many days after checked


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
def _date(value, what: str, eid) -> datetime.date:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(value or "")):
        raise ScanError("exception %s needs %s as YYYY-MM-DD" % (eid, what))
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise ScanError("exception %s: %s %r is not a date" % (eid, what, value))


def check_rules(rules: dict) -> dict:
    """The rules of the policy file, completed with the defaults. Critical and high must be red."""
    rules = dict(DEFAULT_RULES, **(rules or {}))
    for level, value in rules.items():
        if level not in LEVELS or value not in RULE_VALUES:
            raise ScanError("policy rule %s: %s is not allowed (levels %s, values %s)" % (level, value, "/".join(LEVELS), "/".join(RULE_VALUES)))
    weak = [lvl for lvl in RED_LEVELS if rules[lvl] != "red"]
    if weak:
        raise ScanError("policy would weaken the rule: %s must be red" % ", ".join(weak))
    return rules


def check_exception(e: dict) -> None:
    eid = e.get("id")
    if not str(eid or "").strip():
        raise ScanError("an exception needs the advisory id")
    pkgs = e.get("packages")
    if not isinstance(pkgs, list) or not pkgs or not all(str(p).strip() for p in pkgs):
        raise ScanError("exception %s needs the list of packages it applies to" % eid)
    if e.get("kind") not in EXCEPTION_KINDS:
        raise ScanError("exception %s needs kind %s" % (eid, " or ".join(EXCEPTION_KINDS)))
    reason = str(e.get("reason", "")).strip()
    if len(reason) < 20 or not re.search(r"\bbecause\b", reason):
        raise ScanError("exception %s needs a reason that says why (\"does not reach Lanloom because ...\" or "
                        "\"has to wait because ...\")" % eid)
    checked, review = _date(e.get("checked"), "checked", eid), _date(e.get("review_by"), "review_by", eid)
    if review < checked:
        raise ScanError("exception %s: review_by lies before checked" % eid)
    if (review - checked).days > EXCEPTION_MAX_DAYS:
        raise ScanError("exception %s: review_by may be at most %d days after checked" % (eid, EXCEPTION_MAX_DAYS))


def load_policy(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise ScanError("policy %s is not valid JSON: %s" % (path, exc))
    data["rules"] = check_rules(data.get("rules"))
    for e in data.get("exceptions") or []:
        check_exception(e)
    return data


def evaluate(findings: list, policy: dict, today: datetime.date = None) -> dict:
    today = today or datetime.date.today()
    rules = check_rules(policy.get("rules"))
    exceptions = policy.get("exceptions") or []
    used = set()
    red, notices, counted = [], [], {lvl: 0 for lvl in LEVELS}
    for f in sorted(findings, key=lambda f: (LEVELS.index(f["severity"]), f["package"], f["id"])):
        counted[f["severity"]] += 1
        ids = {f["id"], *f.get("aliases", [])}
        exc = next((e for e in exceptions if e["id"] in ids
                    and (not e.get("packages") or f["package"] in e["packages"])), None)
        expired = exc is not None and datetime.date.fromisoformat(exc["review_by"]) < today
        if exc is not None:
            used.add(id(exc))
        if exc and not expired:
            reason = "exception, %s: %s (checked %s, review by %s)" % (exc.get("kind", "not-reachable"), exc["reason"],
                                                                     exc.get("checked", "?"), exc["review_by"])
        elif f["fix_state"] == "fixed" and f["fixed_in"]:
            reason = "fix available: %s" % ", ".join(f["fixed_in"][:3])
        elif f["fix_state"] in ("not-fixed", "wont-fix", "unknown") and f["type"] not in ("python",):
            reason = "no fixed package in the base distribution yet (status %s)" % f["fix_state"]
        else:
            reason = "no fixed version known yet"
        if expired:
            reason += "; the exception expired on %s" % exc["review_by"]
        row = dict(f, reason=reason, excepted=bool(exc and not expired))
        rule = rules[f["severity"]]
        if rule == "red" and not row["excepted"]:
            red.append(row)
        elif rule == "red" or rule == "notice":
            notices.append(row)
    unused = [e["id"] for e in exceptions if id(e) not in used]
    return {"red": red, "notices": notices, "counts": counted, "total": len(findings), "unused_exceptions": unused}


def render(title: str, result: dict, show_unused: bool = False) -> str:
    lines = ["== %s: %d findings (%s)" % (title, result["total"],
             ", ".join("%s %d" % (k, v) for k, v in result["counts"].items() if v) or "none")]
    for row in result["red"]:
        lines.append("RED     %-8s %-22s %-28s %s [%s] - %s" % (row["severity"], row["id"], row["package"], row["version"],
                                                             "/".join(row["platforms"]), row["reason"]))
    for row in result["notices"]:
        lines.append("notice  %-8s %-22s %-28s %s [%s] - %s" % (row["severity"], row["id"], row["package"], row["version"],
                                                             "/".join(row["platforms"]), row["reason"]))
    if show_unused:
        for eid in result.get("unused_exceptions") or []:
            lines.append("unused  exception %s matches no finding any more - remove it from the policy" % eid)
    lines.append("== %s: %s" % (title, "RED (%d critical or high without a valid exception)" % len(result["red"]) if result["red"] else "GREEN"))
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


def _bait_red(result: dict, bait_id: str, level: str) -> bool:
    return any((r["id"] == bait_id or bait_id in r["aliases"]) and r["severity"] == level for r in result["red"])


def cmd_bait(a) -> tuple:
    """A known critical and a known high package must both come back RED from both sources; otherwise the scan or the
    rule is broken (exit 2). The baits are evaluated under the shipped rules, without exceptions."""
    rules = load_policy(a.policy)["rules"]
    baits = ((BAIT_PACKAGE, BAIT_ID, "critical"), (BAIT_HIGH_PACKAGE, BAIT_HIGH_ID, "high"))
    py = evaluate(scan_python([b[0] for b in baits]), {"rules": rules, "exceptions": []})
    gr = None
    if a.tools:
        purl = Path(a.tools) / "bait.purl"
        purl.write_text("".join("pkg:pypi/%s@%s\n" % (b[0][0].lower(), b[0][1]) for b in baits))
        try:
            gr = evaluate(from_grype(run_grype(a.tools, "purl:%s" % purl), "bait"), {"rules": rules, "exceptions": []})
        finally:
            purl.unlink()
    ok = True
    for pkg, bid, level in baits:
        ok_osv = _bait_red(py, bid, level)
        ok_grype = gr is None or _bait_red(gr, bid, level)
        ok = ok and ok_osv and ok_grype
        print("bait %s==%s (%s): OSV %s, grype %s" % (pkg[0], pkg[1], level, "RED (as expected)" if ok_osv else "NOT RED",
                                                    ("RED (as expected)" if ok_grype else "NOT RED") if gr is not None else "skipped"))
    if not ok:
        raise ScanError("self-check failed: a known critical or high package was not reported as RED")
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
    print(render(title, result, show_unused=(a.cmd == "image")))
    if a.json:
        a.json.write_text(json.dumps(dict(result, title=title), indent=1))
    if a.md:
        with open(a.md, "a") as f:
            f.write(markdown(title, result))
    return 1 if result["red"] else 0


if __name__ == "__main__":
    sys.exit(main())
