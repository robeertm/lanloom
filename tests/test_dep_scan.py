"""Policy of the dependency scan (scripts/dep_scan.py), offline: severity, exceptions, an injected vulnerable
package is RED. The live scan (OSV API, grype on the image) runs in CI and in the release gate."""
import datetime
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dep_scan", ROOT / "scripts" / "dep_scan.py")
ds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ds)


@pytest.mark.parametrize("vector,score", [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N", 7.5),
    ("CVSS:3.0/AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:N/A:N", 3.1),
    ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:N/I:N/A:N", 0.0),
])
def test_cvss3_base_score(vector, score):
    assert ds.cvss3_score(vector) == score


def finding(sev, fid="CVE-2026-0001", pkg="libfoo", fix_state="not-fixed", fixed=(), typ="deb"):
    return {"id": fid, "aliases": ["GHSA-aaaa-bbbb-cccc"], "package": pkg, "version": "1.0", "type": typ, "severity": sev,
            "fix_state": fix_state, "fixed_in": list(fixed), "platforms": ["linux/arm64"], "source": "test"}


def test_critical_is_red_high_is_a_notice_with_its_reason():
    res = ds.evaluate([finding("critical"), finding("high", "CVE-2026-0002", fix_state="fixed", fixed=["1.1"]),
                       finding("high", "CVE-2026-0003"), finding("medium", "CVE-2026-0004"), finding("unknown", "X-1")],
                      {"exceptions": []})
    assert [r["id"] for r in res["red"]] == ["CVE-2026-0001"]
    reasons = {r["id"]: r["reason"] for r in res["notices"]}
    assert reasons["CVE-2026-0002"] == "fix available: 1.1"
    assert reasons["CVE-2026-0003"].startswith("no fixed package in the base distribution yet")
    assert "X-1" in reasons and "CVE-2026-0004" not in reasons      # unknown is shown, medium only counted
    assert res["counts"]["medium"] == 1


def test_an_exception_needs_reason_and_date_and_expires(tmp_path):
    exc = {"id": "GHSA-aaaa-bbbb-cccc", "packages": ["libfoo"], "reason": "the code path is not used", "review_by": "2027-01-31"}
    res = ds.evaluate([finding("critical")], {"exceptions": [exc]}, today=datetime.date(2026, 12, 1))
    assert not res["red"] and res["notices"][0]["reason"].startswith("exception: the code path is not used")
    res = ds.evaluate([finding("critical")], {"exceptions": [exc]}, today=datetime.date(2027, 2, 1))
    assert res["red"] and "expired" in res["red"][0]["reason"]
    res = ds.evaluate([finding("critical", pkg="libbar")], {"exceptions": [exc]})
    assert res["red"]                                                  # bound to its packages
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps({"exceptions": [{"id": "CVE-1", "reason": "", "review_by": "2027-01-01"}]}))
    with pytest.raises(ds.ScanError):
        ds.load_policy(bad)


def test_the_shipped_policy_is_valid_and_has_no_expired_exception():
    policy = ds.load_policy(ds.POLICY)
    for e in policy["exceptions"]:
        assert datetime.date.fromisoformat(e["review_by"]) >= datetime.date.today(), e


def test_requirements_are_pinned_and_unpinned_ones_are_refused(tmp_path):
    pkgs = ds.read_requirements(ROOT / "requirements.txt")
    assert len(pkgs) > 15 and ("fastapi", pkgs[0][1]) == pkgs[0]
    p = tmp_path / "r.txt"
    p.write_text("fastapi>=0.1\n")
    with pytest.raises(ds.ScanError):
        ds.read_requirements(p)


OSV_ENTRY = {"id": "GHSA-8q59-q68h-6hv4", "aliases": ["CVE-2020-14343", "PYSEC-2021-142"],
             "database_specific": {"severity": "CRITICAL"},
             "affected": [{"package": {"name": "PyYAML", "ecosystem": "PyPI"}, "ranges": [{"events": [{"introduced": "0"}, {"fixed": "5.4"}]}]}]}
PYSEC_ENTRY = {"id": "PYSEC-2021-142", "aliases": ["CVE-2020-14343", "GHSA-8q59-q68h-6hv4"], "affected": []}


def fake_osv(monkeypatch, vulnerable=(("pyyaml", "5.3.1"),)):
    def http_json(url, body=None, tries=3):
        if url.endswith("/querybatch"):
            return {"results": [{"vulns": [{"id": "GHSA-8q59-q68h-6hv4"}, {"id": "PYSEC-2021-142"}]}
                                if (q["package"]["name"].lower(), q["version"]) in vulnerable else {} for q in body["queries"]]}
        return {"GHSA-8q59-q68h-6hv4": OSV_ENTRY, "PYSEC-2021-142": PYSEC_ENTRY}[url.rsplit("/", 1)[1]]
    monkeypatch.setattr(ds, "_http_json", http_json)


def test_injected_vulnerable_dependency_is_red(monkeypatch, tmp_path, capsys):
    fake_osv(monkeypatch)
    req = tmp_path / "requirements.txt"
    req.write_text((ROOT / "requirements.txt").read_text().replace("PyYAML==", "PyYAML==5.3.1\n# was ") )
    assert ds.main(["python", "--requirements", str(req)]) == 1
    out = capsys.readouterr().out
    assert "RED" in out and "GHSA-8q59-q68h-6hv4" in out and "fix available: 5.4" in out
    assert out.count("GHSA-8q59-q68h-6hv4") == 1                     # the same flaw under two names is one row
    assert ds.main(["python"]) == 0                                    # the real requirements are clean


def test_a_failed_scan_is_never_green(monkeypatch, capsys):
    def broken(url, body=None, tries=3):
        raise ds.ScanError("network down")
    monkeypatch.setattr(ds, "_http_json", broken)
    assert ds.main(["python"]) == 2
    assert "SCAN FAILED" in capsys.readouterr().err


def test_grype_rows_are_merged_across_platforms():
    doc = {"matches": [{"vulnerability": {"id": "CVE-2026-9", "severity": "Critical", "fix": {"state": "wont-fix", "versions": []}},
                        "relatedVulnerabilities": [{"id": "CVE-2026-9"}], "artifact": {"name": "libc6", "version": "2.41", "type": "deb"}}]}
    rows = ds.merge(ds.from_grype(doc, "linux/amd64") + ds.from_grype(doc, "linux/arm64"))
    assert len(rows) == 1 and rows[0]["platforms"] == ["linux/amd64", "linux/arm64"] and rows[0]["severity"] == "critical"
    assert ds.evaluate(rows, {"exceptions": []})["red"]
