"""The landing page (D2): static, self-contained, English, light on the wire, every picture where the page expects it.

* docs/site/index.html carries a content security policy that allows only the own origin, has no inline style, script or
  event handler, and links only to the own repository (and the page's own address);
* every file the page, its style sheet and its script name exists; the tour has a picture for every view in every look;
* the clip is at most 5 MB with a still picture as fallback; the preview image is 1280 x 640 and addressed with ?v=;
* the pictures are not postage stamps (width), phone pictures are real screens (ratio), the folder stays within its budget;
* the stamp script writes the VERSION into the preview addresses; the Pages workflow starts by hand only and pins its actions;
* the README and the tour point at pictures that exist.
"""
import re
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "docs" / "site"
INDEX = SITE / "index.html"
VIEWS = ["overview", "map", "network", "devices", "data-paths", "nightly-reports", "control-room", "firewall", "automations",
         "inventory", "energy", "vulnerabilities", "rewind", "settings"]
LOOKS = [(m, p) for m in ("light", "dark") for p in ("circuit", "aurora")]
PHONE = ["now", "map", "overview", "control-room", "devices"]
PHONE_LOOKS = [("light", "circuit"), ("dark", "aurora")]
MB = 1_000_000
ALLOWED_PREFIXES = ("https://github.com/robeertm/lanloom", "https://raw.githubusercontent.com/robeertm/lanloom/",
                    "https://robeertm.github.io/lanloom/")


def png_size(path: Path):
    d = path.read_bytes()[:32]
    assert d[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", d[16:24])


def html() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_files_exist():
    for name in ("index.html", "site.css", "site.js", "init.js", "social.png", "README.md", "fonts/InterVariable.woff2", "fonts/LICENSE.txt",
                 "img/icon.svg", "media/demo.gif", "media/demo.mp4", "media/demo.webm", "media/demo-still.png"):
        assert (SITE / name).is_file(), name


def test_content_security_policy_and_no_inline_code():
    t = html()
    m = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', t)
    assert m, "the page needs its own content security policy"
    csp = m.group(1)
    assert "default-src 'none'" in csp
    for directive in ("img-src 'self'", "media-src 'self'", "style-src 'self'", "script-src 'self'", "font-src 'self'"):
        assert directive in csp, directive
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "http" not in csp
    assert 'style="' not in t and "<style" not in t.lower(), "no inline style (the policy would block it)"
    assert not re.search(r"<script(?![^>]*\bsrc=)", t, re.I), "no inline script"
    assert not re.search(r"\son[a-z]+\s*=", t, re.I), "no event handler attributes"
    assert 'lang="en"' in t


def test_nothing_is_loaded_from_elsewhere():
    t = html()
    for attr, url in re.findall(r'\b(src|href|poster|srcset)="([^"]+)"', t):
        if re.match(r"(https?:)?//", url):
            if attr in ("src", "poster", "srcset") or url.startswith("//"):
                pytest.fail("%s loads %s from another place" % (attr, url))
            assert url.startswith(ALLOWED_PREFIXES), "link to %s" % url
    css = (SITE / "site.css").read_text(encoding="utf-8")
    for u in re.findall(r"url\(([^)]+)\)", css):
        assert not re.match(r"""['"]?(https?:)?//""", u.strip()), u
    assert "@import" not in css
    js = (SITE / "site.js").read_text(encoding="utf-8") + (SITE / "init.js").read_text(encoding="utf-8")
    for bad in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "http://", "https://", "import("):
        assert bad not in js, bad


def test_every_named_file_exists():
    t = html()
    names = set(re.findall(r'\b(?:src|href|poster)="([^"#?]+)', t))
    for n in names:
        if re.match(r"(https?:|mailto:)", n):
            continue
        assert (SITE / n).exists(), n
    css = (SITE / "site.css").read_text(encoding="utf-8")
    for u in re.findall(r"url\(['\"]?([^)'\"]+)", css):
        assert (SITE / u).exists(), u


def test_tour_has_every_view_in_every_look():
    for v in VIEWS:
        for mode, pal in LOOKS:
            p = SITE / "img" / ("%s-%s-%s.png" % (v, mode, pal))
            assert p.is_file(), p.name
            w, h = png_size(p)
            assert w >= 300 and h >= 300, p.name
    for v in PHONE:
        for mode, pal in PHONE_LOOKS:
            p = SITE / "img" / ("phone-%s-%s-%s.png" % (v, mode, pal))
            assert p.is_file(), p.name
            w, h = png_size(p)
            assert w >= 300 and h / w <= 2.2, "%s is %dx%d: a phone picture shows a real screen" % (p.name, w, h)


def test_the_script_lists_the_same_views_as_the_pictures():
    js = (SITE / "site.js").read_text(encoding="utf-8")
    listed = re.findall(r"^\s*\['([a-z\-]+)', '", js, re.M)
    assert listed == VIEWS


def test_clip_budget_and_still_fallback():
    gif = SITE / "media" / "demo.gif"
    assert gif.stat().st_size <= 5 * MB, "the clip must stay within 5 MB (%d bytes)" % gif.stat().st_size
    assert gif.read_bytes()[:6] in (b"GIF87a", b"GIF89a")
    assert (SITE / "media" / "demo.mp4").stat().st_size <= 3 * MB
    assert (SITE / "media" / "demo.webm").stat().st_size <= 3 * MB
    w, h = png_size(SITE / "media" / "demo-still.png")
    assert w >= 1000
    t = html()
    assert 'poster="media/demo-still.png"' in t, "the still picture is shown before the clip plays"
    assert 'src="media/demo.gif"' in t, "the GIF is the fallback of the video element"
    js = (SITE / "site.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in js and "controls = true" in js
    assert "prefers-reduced-motion" in (SITE / "site.css").read_text(encoding="utf-8")


def test_preview_image_and_version_address():
    assert png_size(SITE / "social.png") == (1280, 640)
    t = html()
    imgs = re.findall(r'content="(https://[^"]*social\.png\?v=[0-9][^"]*)"', t)
    assert len(imgs) == 2, "og:image and twitter:image carry ?v=<version>"
    assert 'property="og:image:width" content="1280"' in t and 'property="og:image:height" content="640"' in t


def test_site_stamp_writes_the_version(tmp_path):
    (tmp_path / "docs" / "site").mkdir(parents=True)
    (tmp_path / "VERSION").write_text("9.8.7\n")
    (tmp_path / "docs" / "site" / "index.html").write_text(html())
    script = ROOT / "scripts" / "site_stamp.py"
    run = lambda *a: subprocess.run([sys.executable, str(script), "--root", str(tmp_path), *a], capture_output=True, text=True)
    assert run("--check").returncode == 1, "an old stamp is found"
    assert run().returncode == 0
    assert run("--check").returncode == 0
    assert (tmp_path / "docs" / "site" / "index.html").read_text().count("social.png?v=9.8.7") == 2


def test_pages_workflow_is_manual_and_pinned():
    wf = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    on = wf.split("\non:", 1)[1].split("\npermissions:", 1)[0]
    assert "workflow_dispatch" in on and "push" not in on and "schedule" not in on and "pull_request" not in on
    for use in re.findall(r"uses:\s*(\S+)", wf):
        assert re.search(r"@[0-9a-f]{40}$", use), "pin %s to a commit" % use
    assert "path: docs/site" in wf and "site_stamp.py" in wf


def test_folder_budget():
    total = sum(p.stat().st_size for p in SITE.rglob("*") if p.is_file())
    assert total <= 14 * MB, "docs/site is %.1f MB" % (total / MB)


def test_readme_and_tour_pictures_exist():
    for doc in ("README.md", "docs/TOUR.md"):
        p = ROOT / doc
        text = p.read_text(encoding="utf-8")
        for src in re.findall(r'<img[^>]*?src="([^"]+)"', text) + re.findall(r"!\[[^\]]*\]\(([^)\s]+)", text):
            if re.match(r"(https?:|data:)", src):
                continue
            assert (p.parent / src.split("#")[0].split("?")[0]).is_file(), "%s: %s" % (doc, src)
    tour = (ROOT / "docs" / "TOUR.md").read_text(encoding="utf-8")
    for v in VIEWS:
        for mode, pal in LOOKS:
            assert "site/img/%s-%s-%s.png" % (v, mode, pal) in tour, (v, mode, pal)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/TOUR.md" in readme and "docs/site/media/demo.gif" in readme
