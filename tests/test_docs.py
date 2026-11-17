"""The documentation holds together (D1).

* Link checker over every Markdown file of the repository: relative links and images resolve, anchors exist (the usual
  rules for heading anchors, plus explicit <a id>), no link is empty. External links are not fetched.
* Every view of the app has a manual page with the common structure; the "About this view" rail points to those pages.
* Troubleshooting has a section for every source and adapter, the compatibility page carries the matrix and the
  "no affiliation or endorsement" line.
* Issue forms: the four kinds exist, each starts with the "never post secrets" notice, nothing leads to a foreign channel.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", ".venv312", "venv", ".pytest_cache", "__pycache__"}

LINK = re.compile(r"(?<!\\)!?\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]*)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
HTML_ID = re.compile(r"""<a\s+(?:[^>]*\s)?(?:id|name)=["']([^"']+)["']""")
FENCE = re.compile(r"^\s*(```|~~~)")


def markdown_files():
    out = []
    for p in sorted(ROOT.rglob("*.md")):
        if SKIP_DIRS & set(p.relative_to(ROOT).parts):
            continue
        out.append(p)
    return out


def slug(title: str) -> str:
    """The anchor of a heading as the repository host builds it: lower case, markup and punctuation removed, spaces to hyphens."""
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", title)          # links keep their text
    t = re.sub(r"<[^>]+>", "", t)                                  # inline HTML
    t = t.replace("`", "").replace("*", "")
    t = t.strip().lower()
    t = re.sub(r"[^\w\- ]", "", t, flags=re.UNICODE)
    return t.replace(" ", "-")


def prose_lines(text: str):
    """Lines outside fenced code blocks."""
    fenced = False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield line


def anchors_of(path: Path) -> set:
    text = path.read_text(encoding="utf-8")
    seen: dict = {}
    out = set()
    for line in prose_lines(text):
        m = HEADING.match(line)
        if m:
            s = slug(m.group(2))
            n = seen.get(s, 0)
            seen[s] = n + 1
            out.add(s if n == 0 else "%s-%d" % (s, n))
        out.update(HTML_ID.findall(line))
    return out


def links_of(path: Path):
    text = path.read_text(encoding="utf-8")
    for no, line in enumerate(prose_lines(text), 1):
        line = re.sub(r"`[^`]*`", "", line)                        # inline code is not a link
        for m in LINK.finditer(line):
            yield no, m.group("target")


def test_there_are_markdown_files():
    names = {p.relative_to(ROOT).as_posix() for p in markdown_files()}
    assert {"README.md", "docs/FAQ.md", "docs/TROUBLESHOOTING.md", "docs/manual/README.md"} <= names


def test_every_relative_link_and_anchor_resolves():
    bad = []
    cache: dict = {}
    for path in markdown_files():
        rel = path.relative_to(ROOT).as_posix()
        for no, target in links_of(path):
            if not target:
                bad.append("%s:%d empty link" % (rel, no))
                continue
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("//"):
                continue                                           # http(s), mailto ...: not fetched here
            file_part, _, frag = target.partition("#")
            dest = path if not file_part else (path.parent / file_part.split("?")[0]).resolve()
            try:
                dest.relative_to(ROOT)
            except ValueError:
                bad.append("%s:%d leaves the repository: %s" % (rel, no, target))
                continue
            if not dest.exists():
                bad.append("%s:%d missing target: %s" % (rel, no, target))
                continue
            if frag and dest.is_file() and dest.suffix == ".md":
                if dest not in cache:
                    cache[dest] = anchors_of(dest)
                if frag.lower() not in cache[dest]:
                    bad.append("%s:%d missing anchor: %s" % (rel, no, target))
    assert not bad, "\n" + "\n".join(bad)


def test_the_link_checker_catches_what_it_should(tmp_path):
    """Bait: a checker that finds nothing is worthless."""
    md = tmp_path / "a.md"
    md.write_text("# Title\n\n## Setting up A / B\n\n[ok](#setting-up-a--b) [bad](#nope) [gone](missing.md)\n"
                  "```\n[in code](#ignored)\n```\n", encoding="utf-8")
    assert "setting-up-a--b" in anchors_of(md)
    targets = [t for _, t in links_of(md)]
    assert targets == ["#setting-up-a--b", "#nope", "missing.md"]
    assert "nope" not in anchors_of(md)


# ---------------------------------------------------------------------------------------------------------------- manual
VIEW_PAGES = {"overview": "overview.md", "map": "map.md", "network": "network.md", "paths": "data-paths.md",
              "devices": "devices.md", "firewall": "firewall.md", "reports": "nightly-reports.md", "automations": "automations.md",
              "settings": "settings.md"}
EXTRA_PAGES = ("inventory.md", "ai.md", "views.md", "README.md")
SECTIONS = ["Purpose", "What you see", "How to use it", "Limits", "Privacy", "See also"]


def test_the_about_rail_points_to_a_page_for_every_view():
    js = (ROOT / "lanloom" / "web" / "js" / "shell.js").read_text(encoding="utf-8")
    views = re.search(r"ABOUT_VIEWS = \[([^\]]*)\]", js).group(1)
    views = re.findall(r"'([a-z]+)'", views)
    assert sorted(views) == sorted(VIEW_PAGES)
    renamed = dict(re.findall(r"(\w+): '([\w-]+)'", re.search(r"MANUAL_PAGE = \{(.*?)\};", js, re.S).group(1)))
    assert {v: renamed.get(v, v) + ".md" for v in views} == VIEW_PAGES
    for page in VIEW_PAGES.values():
        assert (ROOT / "docs" / "manual" / page).is_file(), page


@pytest.mark.parametrize("page", sorted(set(VIEW_PAGES.values()) | {"inventory.md", "ai.md"}))
def test_a_manual_page_has_the_common_structure(page):
    text = (ROOT / "docs" / "manual" / page).read_text(encoding="utf-8")
    h2 = [m.group(2) for m in (HEADING.match(l) for l in prose_lines(text)) if m and m.group(1) == "##"]
    assert h2 == SECTIONS, "%s: %s" % (page, h2)


def test_the_manual_index_lists_every_page():
    index = (ROOT / "docs" / "manual" / "README.md").read_text(encoding="utf-8")
    for page in set(VIEW_PAGES.values()) | set(EXTRA_PAGES) - {"README.md"}:
        assert "(%s" % page in index, page + " missing in docs/manual/README.md"


def test_the_manual_names_the_diagnostic_export():
    assert "Diagnostic export" in (ROOT / "docs" / "manual" / "settings.md").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------------------------- troubleshooting / matrix
def test_troubleshooting_has_a_section_for_every_source_and_adapter():
    text = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    heads = [m.group(2).lower() for m in (HEADING.match(l) for l in prose_lines(text)) if m and m.group(1) == "##"]
    for needed in ("this host", "snmp", "mqtt broker", "switch controller", "home-automation hub", "device-family adapters",
                   "ai services", "the diagnostic export", "scanning and devices"):
        assert needed in heads, needed


def test_every_adapter_family_is_in_the_matrix_and_its_guide_troubleshoots():
    """A new adapter family needs its row (with write column) and a guide with a Troubleshooting section; the shared reasons are
    in TROUBLESHOOTING.md ("Device-family adapters"). Names come from the manifests, so this file names no product."""
    from lanloom import integrations
    text = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    rows = [l for l in (ROOT / "docs" / "COMPATIBILITY.md").read_text(encoding="utf-8").splitlines() if l.startswith("| ")]
    for fam in integrations.families(selectable_only=True):
        name = fam.label.split(" (")[0]
        row = [r for r in rows if r.startswith("| " + name + " ") and r.count("|") >= 6]
        assert row, "%s: no row in the compatibility matrix" % name
        cells = [c.strip() for c in row[0].strip("|").split("|")]
        writes = bool(fam.capabilities.get("write"))
        assert cells[2].lower().startswith("yes" if writes else "none"), "%s: write column does not match the capabilities" % name
        guide = (ROOT / fam.docs).read_text(encoding="utf-8") if fam.docs and (ROOT / fam.docs).is_file() else ""
        assert re.search(r"^## Troubleshooting\s*$", guide, re.M), "%s: its guide needs a Troubleshooting section" % name


def test_every_reason_family_is_explained():
    from lanloom.sources import REASON_KEYS
    text = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8").lower()
    assert "read the status of a source" in text
    for key in REASON_KEYS:
        family = key.split(".")[1]
        assert family in ("hostnet", "hub", "mqtt", "snmp", "controller"), key


def test_compatibility_matrix_and_disclaimer():
    text = (ROOT / "docs" / "COMPATIBILITY.md").read_text(encoding="utf-8")
    assert "## At a glance" in text
    row = [l for l in text.splitlines() if l.startswith("| Family | Read | Write |")]
    assert row and "Checked firmware" in row[0]
    from lanloom import integrations
    names = [f.label.split(" (")[0] for f in integrations.families(selectable_only=True)] + ["SNMP", "MQTT"]
    for fam in names:
        assert any(l.startswith("| " + fam) for l in text.splitlines()), fam
    assert "No affiliation or endorsement" in text


# ----------------------------------------------------------------------------------------------------------- issue forms
FORMS = ("bug_report.yml", "feature_request.yml", "adapter_request.yml", "test_report.yml")
NOTICE = "Never post secrets"


def test_the_four_issue_forms_exist_and_warn_about_secrets():
    folder = ROOT / ".github" / "ISSUE_TEMPLATE"
    for name in FORMS:
        text = (folder / name).read_text(encoding="utf-8")
        assert re.search(r"^name:\s*\S", text, re.M), name
        assert re.search(r"^description:\s*\S", text, re.M), name
        assert re.search(r"^body:\s*$", text, re.M), name
        first = re.search(r"^\s*- type:\s*(\w+)", text, re.M)
        assert first and first.group(1) == "markdown", name + ": the notice comes first"
        assert NOTICE in text, name
        ids = re.findall(r"^\s+id:\s*(\S+)", text, re.M)
        assert len(ids) == len(set(ids)), name + ": duplicate ids"


def test_the_issue_config_has_no_foreign_channels():
    text = (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8")
    assert re.search(r"^blank_issues_enabled:\s*false\s*$", text, re.M)
    for url in re.findall(r"url:\s*(\S+)", text):
        assert url.startswith("https://github.com/robeertm/lanloom/"), url


def test_the_issue_forms_are_valid_yaml():
    yaml = pytest.importorskip("yaml")
    folder = ROOT / ".github" / "ISSUE_TEMPLATE"
    for name in FORMS + ("config.yml",):
        doc = yaml.safe_load((folder / name).read_text(encoding="utf-8"))
        assert isinstance(doc, dict), name
        if name != "config.yml":
            assert doc["body"][0]["type"] == "markdown"
            assert all("type" in item for item in doc["body"])
