#!/usr/bin/env python3
"""Build lanloom/data/vulnfeed.json.gz (the offline vulnerability feed) from public catalogs.

usage:  python3 -I scripts/build_vulnfeed.py --kev known_exploited_vulnerabilities.json --nvd nvd-*.json
                                              [--out lanloom/data/vulnfeed.json.gz] [--date YYYY-MM-DD] [--min-entries N]

Inputs (download them yourself; they are untrusted input and only parsed as JSON, never executed):

* ``--kev``: the catalog of known exploited vulnerabilities (JSON feed of the U.S. Cybersecurity and Infrastructure
  Security Agency, CC0 1.0): https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json
* ``--nvd``: one or more pages of the CVE API 2.0 of the National Vulnerability Database, for example every record in
  the catalog above (``?hasKev``) and the critical ones of the last years (``?cvssV3Severity=CRITICAL`` with
  ``pubStartDate``/``pubEndDate`` periods of at most 120 days). Their ``configurations`` carry the machine-readable
  affected-version ranges (CPE match criteria) the vendors and analysts published.

Only device firmware is kept: a firmware product (``..._firmware``) or a hardware product with its firmware ranges, with
a model name that contains a digit. Operating systems and applications of computers are left out (Lanloom cannot read
their exact build from the network). A record that is in neither the catalog nor at least ``--min-severity`` is left out.

The result is checked with the same validation the app uses (lanloom.vulns.clean_entry) before it is written next to
the old file and swapped in atomically. Lanloom never downloads this file at run time; a new feed arrives with a new
release, or an administrator imports one built with this script (Settings -> Vulnerabilities).
"""
import argparse
import datetime
import gzip
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lanloom import vulns  # noqa: E402

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}
MAX_TITLE = 200
REPLACE_RX = re.compile(r"end[- ]of[- ](life|support|service)|\beol\b|no longer (supported|maintained)|discontinue", re.I)


def read_json(path: Path, what: str):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SystemExit("%s unreadable: %s" % (path.name, type(exc).__name__))
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SystemExit("%s: not a JSON %s" % (path.name, what))


def read_kev(path: Path) -> tuple:
    doc = read_json(path, "catalog")
    if not isinstance(doc, dict) or not isinstance(doc.get("vulnerabilities"), list) or "catalogVersion" not in doc:
        raise SystemExit("%s: unexpected catalog format" % path.name)
    out = {}
    for v in doc["vulnerabilities"]:
        cve = v.get("cveID") if isinstance(v, dict) else None
        if isinstance(cve, str) and vulns.ID_RX.fullmatch(cve):
            out[cve] = v
    if len(out) < 100 and not os.environ.get("VULNFEED_SMALL"):
        raise SystemExit("%s: only %d catalog entries" % (path.name, len(out)))
    return str(doc.get("catalogVersion"))[:20], out


def read_nvd(paths: list) -> dict:
    out = {}
    for p in paths:
        doc = read_json(p, "CVE API page")
        if not isinstance(doc, dict) or doc.get("format") != "NVD_CVE" or not isinstance(doc.get("vulnerabilities"), list):
            raise SystemExit("%s: unexpected CVE API format" % p.name)
        for item in doc["vulnerabilities"]:
            cve = (item or {}).get("cve") if isinstance(item, dict) else None
            if isinstance(cve, dict) and isinstance(cve.get("id"), str) and vulns.ID_RX.fullmatch(cve["id"]):
                out[cve["id"]] = cve
    return out


def severity(cve: dict) -> tuple:
    """(severity, score) from the CVSS metrics, the primary assessment first."""
    metrics = cve.get("metrics") or {}
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV40"):
        rows = metrics.get(key) or []
        rows = sorted(rows, key=lambda r: 0 if r.get("type") == "Primary" else 1)
        for r in rows:
            d = r.get("cvssData") or {}
            sev = str(d.get("baseSeverity") or "").lower()
            score = d.get("baseScore")
            if sev in SEV_ORDER:
                return sev, float(score) if isinstance(score, (int, float)) else None
    for r in metrics.get("cvssMetricV2") or []:
        sev = str(r.get("baseSeverity") or "").lower()
        score = (r.get("cvssData") or {}).get("baseScore")
        if sev in SEV_ORDER:
            return sev, float(score) if isinstance(score, (int, float)) else None
    return "unknown", None


def cpe_fields(criteria: str):
    """(part, vendor, product, version) of a CPE 2.3 name; escaped characters are taken literally."""
    if not isinstance(criteria, str) or not criteria.startswith("cpe:2.3:"):
        return None
    fields, cur, esc = [], "", False
    for ch in criteria[8:]:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            fields.append(cur)
            cur = ""
        else:
            cur += ch
    fields.append(cur)
    if len(fields) < 4:
        return None
    return fields[0], fields[1], fields[2], fields[3]


def version_range(m: dict, version: str) -> list:
    """[] = every version; [range] otherwise; None = cannot be expressed (skipped)."""
    r = {}
    for src, dst in (("versionStartIncluding", "ge"), ("versionStartExcluding", "gt"),
                     ("versionEndIncluding", "le"), ("versionEndExcluding", "lt")):
        v = m.get(src)
        if isinstance(v, str) and v.strip():
            r[dst] = v.strip().replace("\\", "")[:48]
    if r:
        return [r] if all(vulns.version_core(x) for x in r.values()) else None
    if version in ("*", "-", ""):
        return []
    v = version.replace("\\", "")[:48]
    return [{"eq": v}] if vulns.version_core(v) else None


def model_token(product: str) -> str:
    p = re.sub(r"_firmware$", "", product)
    t = vulns.norm_token(p.replace("_", "-"))
    return t if len(t) >= 3 and re.search(r"\d", t) and len(t) <= 60 else ""


def matches_of(cve: dict) -> list:
    """{(vendor, model): [ranges]} -> [{"v", "p", "r"}] from the CPE configurations."""
    found = {}

    def add(vendor, model, ranges):
        v = vulns.norm_token(vendor)
        if not v or not model or len(v) > 60 or ranges is None:
            return
        cur = found.setdefault((v, model), [])
        if cur is ALL:
            return
        if ranges == []:
            found[(v, model)] = ALL
            return
        for r in ranges:
            if r not in cur:
                cur.append(r)

    for cfg in cve.get("configurations") or []:
        nodes = cfg.get("nodes") or []
        hw, fw = [], []
        for node in nodes:
            if node.get("negate"):
                continue
            for m in node.get("cpeMatch") or []:
                f = cpe_fields(m.get("criteria"))
                if not f:
                    continue
                part, vendor, product, version = f
                if part == "h":
                    hw.append((vendor, product, version, m))
                elif part == "o" and m.get("vulnerable"):
                    fw.append((vendor, product, version, m))
        if cfg.get("operator") == "AND" and hw and fw:
            for vendor, product, version, m in fw:
                ranges = version_range(m, version)
                if product.endswith("_firmware"):
                    add(vendor, model_token(product), ranges)
                for hv, hp, hver, hm in hw:            # the platform the firmware runs on
                    if hv == vendor and not hm.get("vulnerable"):
                        add(vendor, model_token(hp), ranges)
        else:
            for vendor, product, version, m in fw:
                if product.endswith("_firmware"):
                    add(vendor, model_token(product), version_range(m, version))
            for vendor, product, version, m in hw:
                if m.get("vulnerable"):
                    add(vendor, model_token(product), [])
    out = []
    for (v, p), rs in sorted(found.items()):
        out.append({"v": v, "p": p, "r": [] if rs is ALL else rs[:vulns.MAX_RANGES]})
    return out[:vulns.MAX_MATCHES]


ALL = object()


def title_of(cve: dict, kev: dict) -> str:
    if kev and kev.get("vulnerabilityName"):
        return str(kev["vulnerabilityName"])[:MAX_TITLE]
    for d in cve.get("descriptions") or []:
        if d.get("lang") == "en" and isinstance(d.get("value"), str):
            text = re.sub(r"\s+", " ", d["value"]).strip()
            first = re.split(r"(?<=[a-z0-9)])\. ", text, maxsplit=1)[0]
            return (first if len(first) <= MAX_TITLE else first[:MAX_TITLE - 1].rsplit(" ", 1)[0] + "…")
    return ""


def build(kev: dict, nvd: dict, min_sev: str) -> list:
    entries = []
    for cid, cve in sorted(nvd.items()):
        k = kev.get(cid)
        sev, score = severity(cve)
        if not k and SEV_ORDER.get(sev, 9) > SEV_ORDER[min_sev]:
            continue
        if str(cve.get("vulnStatus") or "").lower() in ("rejected", "reserved"):
            continue
        ms = matches_of(cve)
        if not ms:
            continue
        hint = " ".join(str((k or {}).get(x) or "") for x in ("requiredAction", "shortDescription", "notes"))
        all_versions = all(not m["r"] for m in ms)
        action = "replace" if k and REPLACE_RX.search(hint) and all_versions else "update"
        raw = {"id": cid, "sev": sev, "score": score, "kev": (k or {}).get("dateAdded") or "",
               "ransom": str((k or {}).get("knownRansomwareCampaignUse") or "").lower() == "known",
               "title": title_of(cve, k), "action": action, "match": ms}
        e = vulns.clean_entry(raw)
        if e is None:
            continue
        entries.append(e)
    return entries


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kev", type=Path, required=True)
    ap.add_argument("--nvd", type=Path, nargs="+", required=True)
    ap.add_argument("--out", type=Path, default=ROOT / "lanloom" / "data" / "vulnfeed.json.gz")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--min-severity", default="critical", choices=list(SEV_ORDER)[:4])
    ap.add_argument("--min-entries", type=int, default=300)
    a = ap.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.date):
        raise SystemExit("--date must be YYYY-MM-DD")
    kev_version, kev = read_kev(a.kev)
    nvd = read_nvd(a.nvd)
    missing = [c for c in kev if c not in nvd]
    entries = build(kev, nvd, a.min_severity)
    n_kev = sum(1 for e in entries if e["kev"])
    if len(entries) < a.min_entries:
        raise SystemExit("only %d entries (expected at least %d)" % (len(entries), a.min_entries))
    meta = {"as_of": a.date, "sources": ["known-exploited catalog %s" % kev_version, "national vulnerability database %s" % a.date]}
    note = "Advisory only. Affected firmware ranges of device models from public catalogs; a match is a hint, never a verdict."
    a.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = a.out.with_name(a.out.name + ".new")
    vulns.write_feed(tmp, meta, entries, note)               # a header line, then one entry per line (read with little memory)
    data = tmp.read_bytes()
    back, check = vulns.parse_feed(data)                       # the app must read back exactly what was written
    if back["entries"] != len(entries) or back["skipped"]:
        tmp.unlink()
        raise SystemExit("read-back check failed (%d of %d entries)" % (back["entries"], len(entries)))
    os.replace(tmp, a.out)
    models = sum(len(e["match"]) for e in entries)
    print("wrote %s: %d entries (%d known exploited), %d model matches, %d bytes; %d catalog entries without a CVE API record"
          % (a.out.name, len(entries), n_kev, models, len(data), len(missing)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
