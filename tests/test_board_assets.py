"""Board assets: font + licence shipped, theme tokens complete for light/dark/auto, CSS only uses defined tokens,
engine files are English-only."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "lanloom", "web")


def read(*p):
    with open(os.path.join(WEB, *p), encoding="utf-8") as f:
        return f.read()


def test_font_and_licence():
    assert os.path.getsize(os.path.join(WEB, "fonts", "InterVariable.woff2")) > 100_000
    lic = read("fonts", "LICENSE.txt")
    assert "SIL Open Font License" in lic
    assert "InterVariable.woff2" in read("css", "tokens.css")


def blocks(css):
    light = re.search(r":root,\s*\[data-theme=\"light\"\]\s*\{(.*?)\n\}", css, re.S).group(1)
    dark = re.search(r"\n\[data-theme=\"dark\"\]\s*\{(.*?)\n\}", css, re.S).group(1)
    auto = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{\s*\[data-theme=\"auto\"\]\s*\{(.*?)\n  \}", css, re.S).group(1)
    decl = lambda b: dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", b))
    return decl(light), decl(dark), decl(auto)


def test_theme_tokens_complete():
    light, dark, auto = blocks(read("css", "tokens.css"))
    assert dark == auto, "dark and auto (dark OS) token sets must be identical"
    colour = {k for k, v in light.items() if re.match(r"#|rgba?\(", v.strip())}
    missing = colour - set(dark)
    assert not missing, missing
    for k in ("--ll-pcb-trace-uplink", "--ll-pcb-board", "--ll-text", "--ll-space-4", "--ll-radius-md", "--ll-fs-md", "--ll-font"):
        assert k in light


def test_board_css_uses_defined_tokens():
    light, _, _ = blocks(read("css", "tokens.css"))
    used = set(re.findall(r"var\((--[\w-]*\w)\b", read("css", "board.css")))
    js = read("board", "board.js")
    used |= {v for v in re.findall(r"var\((--[\w-]+)", js) if not v.endswith("-")}   # dynamic names are added below
    used |= {"--ll-pcb-trace-" + k for k in ("uplink", "lan", "service", "flow", "integration")}
    assert used <= set(light), used - set(light)


def _contrast(a, b):
    def lum(h):
        h = h.lstrip("#")
        c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_text_contrast_wcag_aa():
    light, dark, _ = blocks(read("css", "tokens.css"))
    for theme in (light, dark):
        for fg in ("--ll-text", "--ll-text-2", "--ll-text-3"):
            for bg in ("--ll-pcb-tile", "--ll-surface"):
                assert _contrast(theme[fg].strip(), theme[bg].strip()) >= 4.5, (fg, bg)
        for bg in ("--ll-pcb-zone", "--ll-pcb-board"):
            assert _contrast(theme["--ll-pcb-silk"].strip(), theme[bg].strip()) >= 4.5
        assert _contrast(theme["--ll-pcb-badge-text"].strip(), theme["--ll-pcb-badge-bg"].strip()) >= 4.5


def test_engine_files_english_only():
    for name in ("router.js", "layout.js", "board.js", "dots.js", "icons.js"):
        src = read("board", name)
        assert not re.search(r"[äöüÄÖÜß]", src), name
    for name in ("tokens.css", "board.css"):
        assert not re.search(r"[äöüÄÖÜß]", read("css", name)), name
