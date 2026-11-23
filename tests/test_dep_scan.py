"""Policy of the dependency scan (scripts/dep_scan.py), offline: severity, exceptions, an injected vulnerable
package is RED. Critical and high are both red without a valid exception (B10, same rule as the shared release gate);
the policy file cannot lower them. The live scan (OSV API, grype on the image) runs in CI and in the release gate."""
import datetime
import importlib.util
import json
import re
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


EXC = {"id": "GHSA-aaaa-bbbb-cccc", "packages": ["libfoo"], "kind": "not-reachable",
       "reason": "does not reach Lanloom because the code path is never used", "checked": "2026-11-23", "review_by": "2027-01-31"}


def test_critical_and_high_are_red_without_an_exception():
    res = ds.evaluate([finding("critical"), finding("high", "CVE-2026-0002", fix_state="fixed", fixed=["1.1"]),
                       finding("high", "CVE-2026-0003"), finding("medium", "CVE-2026-0004"), finding("low", "CVE-2026-0005"),
                       finding("unknown", "X-1")], {"exceptions": []})
    assert [r["id"] for r in res["red"]] == ["CVE-2026-0001", "CVE-2026-0002", "CVE-2026-0003"]
    reasons = {r["id"]: r["reason"] for r in res["red"]}
    assert reasons["CVE-2026-0002"] == "fix available: 1.1"
    assert reasons["CVE-2026-0003"].startswith("no fixed package in the base distribution yet")
    notices = {r["id"] for r in res["notices"]}
    assert notices == {"X-1"}                                          # unknown is shown, medium and low only counted
    assert res["counts"]["medium"] == 1 and res["counts"]["low"] == 1


def test_a_high_finding_with_a_valid_exception_is_green_with_a_notice():
    res = ds.evaluate([finding("high")], {"exceptions": [EXC]}, today=datetime.date(2026, 12, 1))
    assert not res["red"]
    row = res["notices"][0]
    assert row["excepted"] and row["reason"].startswith("exception, not-reachable: does not reach Lanloom because")
    assert "checked 2026-11-23, review by 2027-01-31" in row["reason"]
    assert res["unused_exceptions"] == []


def test_an_expired_exception_counts_as_missing_for_high_and_critical():
    for sev in ("high", "critical"):
        res = ds.evaluate([finding(sev)], {"exceptions": [EXC]}, today=datetime.date(2027, 2, 1))
        assert res["red"] and "expired on 2027-01-31" in res["red"][0]["reason"], sev
    res = ds.evaluate([finding("critical")], {"exceptions": [EXC]}, today=datetime.date(2026, 12, 1))
    assert not res["red"]                                              # critical: same rule as before
    res = ds.evaluate([finding("high", pkg="libbar")], {"exceptions": [EXC]})
    assert res["red"] and res["unused_exceptions"] == ["GHSA-aaaa-bbbb-cccc"]   # bound to its packages


@pytest.mark.parametrize("change,message", [
    ({"reason": ""}, "reason"),
    ({"reason": "the code path is never used here"}, "reason"),            # no "because": does not say why
    ({"packages": []}, "packages"),
    ({"packages": None}, "packages"),
    ({"kind": "accepted"}, "kind"),
    ({"kind": None}, "kind"),
    ({"checked": None}, "checked"),
    ({"review_by": "2027-13-01"}, "review_by"),
    ({"review_by": "2026-11-01"}, "before checked"),
    ({"review_by": "2027-05-26"}, "183 days"),
    ({"id": ""}, "advisory id"),
])
def test_an_exception_without_id_packages_kind_reason_or_dates_is_refused(tmp_path, change, message):
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps({"exceptions": [dict(EXC, **change)]}))
    with pytest.raises(ds.ScanError, match=message):
        ds.load_policy(bad)
    good = tmp_path / "ok.json"
    good.write_text(json.dumps({"exceptions": [dict(EXC, review_by="2027-05-25")]}))   # 183 days: allowed
    assert ds.load_policy(good)["exceptions"]


@pytest.mark.parametrize("rules", [{"high": "notice"}, {"critical": "count"}, {"high": "count", "critical": "red"},
                                   {"medium": "loud"}, {"severe": "red"}])
def test_the_policy_file_cannot_lower_critical_or_high(tmp_path, monkeypatch, capsys, rules):
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps({"rules": rules, "exceptions": []}))
    with pytest.raises(ds.ScanError):
        ds.load_policy(bad)
    with pytest.raises(ds.ScanError):
        ds.evaluate([finding("high")], {"rules": rules, "exceptions": []})
    fake_osv(monkeypatch)
    assert ds.main(["python", "--policy", str(bad)]) == 2               # a weakened policy is a failed scan, never green
    assert "SCAN FAILED" in capsys.readouterr().err


def test_a_stricter_policy_is_allowed():
    res = ds.evaluate([finding("medium")], {"rules": {"medium": "red"}, "exceptions": []})
    assert res["red"]


def test_the_shipped_policy_is_valid_and_has_no_expired_exception():
    policy = ds.load_policy(ds.POLICY)
    assert policy["rules"]["critical"] == policy["rules"]["high"] == "red"
    assert policy["rules"]["unknown"] == "notice"
    raw = json.loads(ds.POLICY.read_text())
    assert raw["rules"]["high"] == "red" and "high findings are red" in raw["about"].lower().replace("critical and ", "")
    for e in policy["exceptions"]:
        assert datetime.date.fromisoformat(e["review_by"]) >= datetime.date.today(), e
        assert e["packages"] and e["kind"] in ds.EXCEPTION_KINDS and "because" in e["reason"], e
    ids = [e["id"] for e in policy["exceptions"]]
    assert len(ids) == len(set(ids)), "one entry per advisory"


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


HIGH_ENTRY = {"id": "GHSA-q2q7-5pp4-w6pg", "aliases": ["CVE-2021-33503"], "database_specific": {"severity": "HIGH"},
              "affected": [{"package": {"name": "urllib3", "ecosystem": "PyPI"}, "ranges": [{"events": [{"introduced": "0"}, {"fixed": "1.26.5"}]}]}]}


def fake_osv_high(monkeypatch, high_label="HIGH"):
    entry = dict(HIGH_ENTRY, database_specific={"severity": high_label})

    def http_json(url, body=None, tries=3):
        if url.endswith("/querybatch"):
            out = []
            for q in body["queries"]:
                key = (q["package"]["name"].lower(), q["version"])
                out.append({"vulns": [{"id": "GHSA-8q59-q68h-6hv4"}]} if key == ("pyyaml", "5.3.1") else
                           {"vulns": [{"id": "GHSA-q2q7-5pp4-w6pg"}]} if key == ("urllib3", "1.26.4") else {})
            return {"results": out}
        return {"GHSA-8q59-q68h-6hv4": OSV_ENTRY, "GHSA-q2q7-5pp4-w6pg": entry}[url.rsplit("/", 1)[1]]
    monkeypatch.setattr(ds, "_http_json", http_json)


def test_injected_high_dependency_is_red_and_a_valid_exception_makes_it_a_notice(monkeypatch, tmp_path, capsys):
    fake_osv_high(monkeypatch)
    req = tmp_path / "requirements.txt"
    req.write_text((ROOT / "requirements.txt").read_text() + "urllib3==1.26.4\n")
    assert ds.main(["python", "--requirements", str(req)]) == 1
    out = capsys.readouterr().out
    assert "RED     high" in out and "GHSA-q2q7-5pp4-w6pg" in out and "RED (1 critical or high" in out
    pol = tmp_path / "p.json"
    exc = dict(EXC, id="CVE-2021-33503", packages=["urllib3"], checked=datetime.date.today().isoformat(),
               review_by=(datetime.date.today() + datetime.timedelta(days=30)).isoformat())
    pol.write_text(json.dumps({"rules": {"critical": "red", "high": "red"}, "exceptions": [exc]}))
    assert ds.main(["python", "--requirements", str(req), "--policy", str(pol)]) == 0
    out = capsys.readouterr().out
    assert "notice  high" in out and "exception, not-reachable" in out and out.rstrip().endswith("GREEN")


def test_the_self_check_needs_a_red_critical_and_a_red_high_bait(monkeypatch, capsys):
    fake_osv_high(monkeypatch)
    assert ds.main(["bait"]) == 0
    out = capsys.readouterr().out
    assert "urllib3==1.26.4 (high): OSV RED (as expected)" in out and "PyYAML==5.3.1 (critical): OSV RED" in out
    fake_osv_high(monkeypatch, high_label="MODERATE")                   # the high bait comes back lower: scan or rule broken
    assert ds.main(["bait"]) == 2
    assert "self-check failed" in capsys.readouterr().err


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


def test_unused_exceptions_are_listed_in_the_image_scan(monkeypatch, tmp_path, capsys):
    pol = tmp_path / "p.json"
    pol.write_text(json.dumps({"exceptions": [EXC]}))
    doc = {"matches": [{"vulnerability": {"id": "CVE-2026-8", "severity": "Medium", "fix": {"state": "not-fixed"}},
                        "artifact": {"name": "libbaz", "version": "1", "type": "deb"}}]}
    monkeypatch.setattr(ds, "run_grype", lambda tools, src, platform=None: doc)
    assert ds.main(["image", "docker:x", "--tools", str(tmp_path), "--policy", str(pol)]) == 0
    assert "unused  exception GHSA-aaaa-bbbb-cccc matches no finding any more" in capsys.readouterr().out


def test_the_documentation_lists_exactly_the_exceptions_of_the_policy():
    doc = (ROOT / "docs" / "DEPENDENCY-SCAN.md").read_text()
    table = doc.split("## Today's exceptions", 1)[1].split("\n## ", 1)[0]
    listed = re.findall(r"^\| ((?:CVE|GHSA|PYSEC)-[^ |]+) \|", table, re.M)
    policy = ds.load_policy(ds.POLICY)
    assert sorted(listed) == sorted(e["id"] for e in policy["exceptions"])
    for e in policy["exceptions"]:
        assert re.search(r"^\| %s \| %s \| %s \| %s \|$" % (re.escape(e["id"]), re.escape(", ".join(e["packages"])),
                                                         e["kind"], e["review_by"]), table, re.M), e["id"]
    for name in ("SECURITY.md", "docs/SECURITY-MODEL.md"):
        text = (ROOT / name).read_text()
        assert "critical or high" in text and "DEPENDENCY-SCAN.md" in text, name
