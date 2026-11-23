"""Colour themes (accent palettes): every palette sets the complete token set for light, dark and auto, text and
graphics meet WCAG AA in every palette and mode, no light surface is dark and no dark surface is light, meaning colours
stay recognisable, and no component carries a colour of its own."""
import colorsys
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "lanloom", "web")
DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")


def read(*p):
    with open(os.path.join(WEB, *p), encoding="utf-8") as f:
        return f.read()


def _decl(block):
    return {k: v.strip() for k, v in DECL.findall(block)}


def _base():
    css = read("css", "tokens.css")
    light = re.search(r":root,\s*\[data-theme=\"light\"\]\s*\{(.*?)\n\}", css, re.S).group(1)
    dark = re.search(r"\n\[data-theme=\"dark\"\]\s*\{(.*?)\n\}", css, re.S).group(1)
    return _decl(light), {**_decl(light), **_decl(dark)}


def _palette_ids():
    m = re.search(r"LL\.PALETTES\s*=\s*\[([^\]]*)\]", read("js", "app.js"))
    return re.findall(r"'([\w-]+)'", m.group(1))


def _palette_sets(pid):
    """(light, dark, auto) token sets of one non-default palette, parsed from palettes.css."""
    css = read("css", "palettes.css")
    sel = r'\[data-palette="%s"\]' % pid
    light = re.search(r"\n" + sel + r",\n" + sel + r' \[data-theme="light"\]:not\(\[data-palette\]\) \{(.*?)\n\}', css, re.S)
    dark = re.search(r"\n" + sel + r'\[data-theme="dark"\],\n' + sel + r' \[data-theme="dark"\]:not\(\[data-palette\]\) \{(.*?)\n\}', css, re.S)
    auto = re.search(r"@media \(prefers-color-scheme: dark\) \{\s*" + sel + r'\[data-theme="auto"\],\s*' + sel
                     + r' \[data-theme="auto"\]:not\(\[data-palette\]\) \{(.*?)\n  \}', css, re.S)
    assert light and dark and auto, "palette %s must define light, dark and auto blocks in the documented form" % pid
    return _decl(light.group(1)), _decl(dark.group(1)), _decl(auto.group(1))


def _modes():
    """[(label, effective token set)] for every palette and mode."""
    base_light, base_dark = _base()
    out = [("circuit/light", base_light), ("circuit/dark", base_dark)]
    for pid in _palette_ids():
        if pid == "circuit":
            continue
        light, dark, auto = _palette_sets(pid)
        out += [(pid + "/light", {**base_light, **light}), (pid + "/dark", {**base_dark, **dark}), (pid + "/auto-dark", {**base_dark, **auto})]
    return out


MODES = _modes()
IDS = [m[0] for m in MODES]


# ------------------------------------------------------------------ colour maths
def _rgba(v):
    v = v.strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return [int(h[i:i + 2], 16) for i in (0, 2, 4)] + [1.0]
    m = re.match(r"rgba?\(([^)]*)\)", v)
    parts = [p.strip() for p in m.group(1).split(",")]
    return [float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]) if len(parts) > 3 else 1.0]


def _over(fg, bg):
    """Composite fg (maybe translucent) over the opaque colour bg."""
    f, b = _rgba(fg), _rgba(bg)
    a = f[3]
    return "#%02X%02X%02X" % tuple(round(f[i] * a + b[i] * (1 - a)) for i in range(3))


def _lum(v):
    c = [x / 255 for x in _rgba(v)[:3]]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _hue(v):
    r, g, b, _ = _rgba(v)
    return colorsys.rgb_to_hls(r / 255, g / 255, b / 255)[0] * 360


# ------------------------------------------------------------------ structure
def test_palette_list_matches_css_and_strings():
    ids = _palette_ids()
    assert ids[0] == "circuit" and len(ids) >= 2, "Circuit green stays first (the default) and at least one more palette exists"
    en = json.load(open(os.path.join(ROOT, "lanloom", "i18n", "en.json"), encoding="utf-8"))
    de = json.load(open(os.path.join(ROOT, "lanloom", "i18n", "de.json"), encoding="utf-8"))
    for pid in ids:
        for k in ("palette." + pid, "palette." + pid + ".desc"):
            assert en.get(k) and de.get(k), k
    css = read("css", "palettes.css")
    declared = set(re.findall(r'^\[data-palette="([\w-]+)"\]', css, re.M))
    assert declared == set(ids) - {"circuit"}, "every palette but the default has rules, and every rule belongs to a listed palette"


def test_default_palette_has_no_override():
    assert "circuit" not in read("css", "palettes.css").replace("Circuit green", "")


def test_palettes_load_after_tokens_and_are_served():
    html = read("index.html")
    assert html.index("css/tokens.css") < html.index("css/palettes.css") < html.index("css/board.css")
    assert "css/palettes.css" in open(os.path.join(ROOT, "lanloom", "main.py"), encoding="utf-8").read()


@pytest.mark.parametrize("pid", [p for p in _palette_ids() if p != "circuit"])
def test_palette_sets_every_colour_token(pid):
    base_light, _ = _base()
    keys = {k for k, v in base_light.items() if re.match(r"#|rgba?\(", v) or "shadow" in k or k in ("--ll-pcb-trace-rest", "--ll-pcb-led-glow")}
    light, dark, auto = _palette_sets(pid)
    assert dark == auto, "dark and auto (dark system setting) must be identical"
    for name, block in (("light", light), ("dark", dark)):
        assert set(block) == keys, (name, keys ^ set(block))


# ------------------------------------------------------------------ contrast
TEXT_ON = {"--ll-text": ("--ll-bg", "--ll-surface", "--ll-surface-2", "--ll-surface-3", "--ll-pcb-tile", "--ll-pcb-zone", "--ll-pcb-board"),
           "--ll-text-2": ("--ll-bg", "--ll-surface", "--ll-surface-2", "--ll-surface-3", "--ll-pcb-tile"),
           "--ll-text-3": ("--ll-bg", "--ll-surface", "--ll-surface-2", "--ll-pcb-tile"),
           "--ll-pcb-silk": ("--ll-pcb-zone", "--ll-pcb-board"),
           "--ll-pcb-silk-2": ("--ll-pcb-zone", "--ll-pcb-board"),
           "--ll-accent-text": ("--ll-bg", "--ll-surface", "--ll-surface-2"),
           "--ll-ok-text": ("--ll-surface", "--ll-bg"), "--ll-warn-text": ("--ll-surface", "--ll-bg"),
           "--ll-danger-text": ("--ll-surface", "--ll-bg"), "--ll-pcb-badge-text": ("--ll-pcb-badge-bg",),
           "--ll-on-accent": ("--ll-accent", "--ll-accent-hover")}
SOFT_TEXT = {"--ll-accent-soft": "--ll-accent-text", "--ll-ok-soft": "--ll-ok-text", "--ll-warn-soft": "--ll-warn-text", "--ll-danger-soft": "--ll-danger-text"}


@pytest.mark.parametrize("label,tok", MODES, ids=IDS)
def test_text_contrast_wcag_aa(label, tok):
    for fg, backs in TEXT_ON.items():
        for bg in backs:
            assert contrast(_over(tok[fg], tok["--ll-surface"]), _over(tok[bg], tok["--ll-surface"])) >= 4.5, (label, fg, bg)
    for soft, fg in SOFT_TEXT.items():
        ground = _over(tok[soft], tok["--ll-surface"])
        assert contrast(tok[fg], ground) >= 4.5, (label, fg, soft)


GRAPHICS = ("--ll-accent", "--ll-focus", "--ll-ok", "--ll-warn", "--ll-danger", "--ll-grade-realtime", "--ll-grade-near", "--ll-grade-periodic",
            "--ll-grade-sluggish", "--ll-grade-interrupted", "--ll-grade-unknown", "--ll-pcb-trace-uplink", "--ll-pcb-trace-lan",
            "--ll-pcb-trace-service", "--ll-pcb-trace-flow", "--ll-pcb-trace-integration", "--ll-pcb-trace-cable", "--ll-pcb-trace-radio",
            "--ll-pcb-trace-virtual", "--ll-pcb-copper")
GRAPHIC_GROUNDS = ("--ll-surface", "--ll-pcb-board", "--ll-pcb-zone", "--ll-pcb-tile")


@pytest.mark.parametrize("label,tok", MODES, ids=IDS)
def test_graphics_contrast_at_least_3(label, tok):
    for fg in GRAPHICS:
        for bg in GRAPHIC_GROUNDS:
            assert contrast(tok[fg], _over(tok[bg], tok["--ll-surface"])) >= 3.0, (label, fg, bg, round(contrast(tok[fg], _over(tok[bg], tok["--ll-surface"])), 2))


GLASS = [(label, tok) for label, tok in MODES if label.split("/")[0] in ("summerglass", "winterglass")]


@pytest.mark.parametrize("label,tok", GLASS, ids=[m[0] for m in GLASS])
def test_glass_boxes_stand_out_from_the_page(label, tok):
    """The glass family (SummerGlass, WinterGlass) exists so that a box is told apart from the page at a glance: the box itself differs from the ground,
    its edge is a visible line, and the outline of fields and buttons reaches the 3:1 of WCAG 2.2 SC 1.4.11."""
    checks = (("--ll-surface", "--ll-bg", 1.3), ("--ll-border", "--ll-surface", 3.0), ("--ll-border", "--ll-bg", 1.8),
              ("--ll-border-strong", "--ll-surface-2", 3.0), ("--ll-border-strong", "--ll-bg", 3.0),
              ("--ll-pcb-tile", "--ll-pcb-board", 1.3), ("--ll-pcb-tile-line", "--ll-pcb-tile", 3.0))
    for fg, bg, need in checks:
        got = contrast(tok[fg], _over(tok[bg], tok["--ll-surface"]))
        assert got >= need, (label, fg, bg, round(got, 2))


def _glass_extras():
    """(light, dark, auto) glass tokens of the glass family (the light behind the glass, the fill, sheen, glare and gloss)."""
    css = read("css", "palettes.css")
    g = re.escape('html:is([data-palette="summerglass"], [data-palette="winterglass"])')
    light = re.search(g + r",\n" + g + r' \[data-theme="light"\]:not\(\[data-palette\]\) \{(.*?)\n\}', css, re.S)
    dark = re.search(g + r'\[data-theme="dark"\],\n' + g + r' \[data-theme="dark"\]:not\(\[data-palette\]\) \{(.*?)\n\}', css, re.S)
    auto = re.search(g + r'\[data-theme="auto"\],\s*' + g + r' \[data-theme="auto"\]:not\(\[data-palette\]\) \{(.*?)\n  \}', css, re.S)
    assert light and dark and auto, "the glass tokens are set for light, dark and auto"
    return _decl(light.group(1)), _decl(dark.group(1)), _decl(auto.group(1))


@pytest.mark.parametrize("label,tok", GLASS, ids=[m[0] for m in GLASS])
def test_text_on_glass_stays_readable_over_every_tint(label, tok):
    """The light behind the glass shines through: every colour field (alone and over both ends of the gradient) is composited
    under the glass fill, and text, links and the edges of boxes keep their contrast on every result."""
    light, dark, auto = _glass_extras()
    assert dark == auto
    g = light if label.endswith("/light") else dark
    grounds = []
    for base in (g["--ll-glass-top"], g["--ll-glass-bottom"]):
        grounds.append(base)
        grounds += [_over(g["--ll-glass-tint-%d" % i], base) for i in (1, 2, 3)]
    for page in grounds:
        box = _over(g["--ll-glass-fill"], page)
        for fg, need in (("--ll-text", 7), ("--ll-text-2", 4.5), ("--ll-text-3", 4.5), ("--ll-accent-text", 4.5), ("--ll-danger-text", 4.5),
                         ("--ll-warn-text", 4.5), ("--ll-ok-text", 4.5)):
            assert contrast(tok[fg], box) >= need, (label, fg, "on glass", page, round(contrast(tok[fg], box), 2))
        for fg, need in (("--ll-text", 7), ("--ll-text-2", 4.5), ("--ll-text-3", 4.5), ("--ll-accent-text", 4.5)):
            assert contrast(tok[fg], page) >= need, (label, fg, "on the page", page, round(contrast(tok[fg], page), 2))
        assert contrast(box, page) >= 1.15, (label, "a pane stands apart from the light behind it", page, round(contrast(box, page), 2))
        assert contrast(tok["--ll-border"], box) >= 2.4, (label, "the edge of a pane", page, round(contrast(tok["--ll-border"], box), 2))
        assert contrast(tok["--ll-on-accent"], _over(g["--ll-glass-gloss"], tok["--ll-accent"])) >= 3, (label, "button label under the top of the gloss")


def test_glass_needs_no_blur():
    css = read("css", "glass.css")
    assert "backdrop-filter" not in css and "filter:" not in css, "the glass is light and layers, never a blur (budget of small machines)"
    assert "prefers-reduced-motion" in css and "transition" in css
    html = read("index.html")
    assert html.index("css/app.css") < html.index("css/energy.css") < html.index("css/glass.css") < html.index("css/handy.css"), \
        "the glass is laid over the components, so it loads after them, and before the phone rules, which come last"


def test_glass_family_is_listed_and_shares_its_blocks():
    ids = _palette_ids()
    assert "summerglass" in ids and "winterglass" in ids and len(GLASS) == 6
    assert _palette_sets("summerglass") == _palette_sets("winterglass"), "one family: the two names are its light and its dark face"


# ------------------------------------------------------------------ surfaces (no dark islands in light, no light islands in dark)
SURFACES = ("--ll-bg", "--ll-surface", "--ll-surface-2", "--ll-surface-3", "--ll-pcb-board", "--ll-pcb-zone", "--ll-pcb-tile", "--ll-pcb-icon-bg",
            "--ll-pcb-badge-bg", "--ll-pcb-legend-bg")


@pytest.mark.parametrize("label,tok", MODES, ids=IDS)
def test_surfaces_stay_in_their_mode(label, tok):
    dark = "dark" in label
    ground = tok["--ll-bg"]
    for k in SURFACES:
        lum = _lum(_over(tok[k], ground))
        assert (lum <= 0.06) if dark else (lum >= 0.6), (label, k, round(lum, 3))
    # the text sits on the right side of every surface
    for k in SURFACES:
        assert contrast(tok["--ll-text"], _over(tok[k], ground)) >= 7, (label, k)


# ------------------------------------------------------------------ meaning colours
@pytest.mark.parametrize("label,tok", MODES, ids=IDS)
def test_meaning_colours_stay_recognisable(label, tok):
    for k in ("--ll-danger", "--ll-danger-text", "--ll-grade-interrupted"):
        h = _hue(tok[k])
        assert h <= 12 or h >= 348, (label, k, round(h))
    for k in ("--ll-warn", "--ll-warn-text", "--ll-grade-periodic"):
        assert 30 <= _hue(tok[k]) <= 50, (label, k, round(_hue(tok[k])))
    assert 18 <= _hue(tok["--ll-grade-sluggish"]) <= 32, label
    for k in ("--ll-ok", "--ll-ok-text", "--ll-grade-realtime"):
        assert 120 <= _hue(tok[k]) <= 175, (label, k, round(_hue(tok[k])))     # green to blue-green, never blue or violet
    # trace kinds stay apart (cable and LAN are the same wire on purpose)
    kinds = {k[len("--ll-pcb-trace-"):]: tok[k] for k in tok if k.startswith("--ll-pcb-trace-") and k != "--ll-pcb-trace-rest"}
    kinds.pop("cable")
    names = sorted(kinds)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ra, rb = _rgba(kinds[a]), _rgba(kinds[b])
            assert sum(abs(ra[j] - rb[j]) for j in range(3)) >= 60, (label, a, b)


def test_a_palette_is_not_a_copy_of_the_default():
    base_light, base_dark = _base()
    for label, tok in MODES:
        if label.startswith("circuit"):
            continue
        base = base_dark if "dark" in label else base_light
        same = [k for k in ("--ll-accent", "--ll-bg", "--ll-pcb-trace-lan", "--ll-pcb-board") if tok[k].lower() == base[k].lower()]
        assert not same, (label, same)


# ------------------------------------------------------------------ no colour of its own in components
COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|\boklch\(|\blab\(")
FULLY_TRANSPARENT = "rgba(0,0,0,0)"      # the far end of a fade, invisible by definition


def _component_files():
    for sub, ext in (("css", ".css"), ("board", ".css"), ("js", ".js"), ("board", ".js")):
        d = os.path.join(WEB, sub)
        for n in sorted(os.listdir(d)):
            if n.endswith(ext) and n not in ("tokens.css", "palettes.css"):
                yield sub + "/" + n


def test_components_carry_no_colour_of_their_own():
    bad = []
    for rel in _component_files():
        for no, line in enumerate(read(*rel.split("/")).splitlines(), 1):
            line = line.replace(FULLY_TRANSPARENT, "")
            if not COLOUR_LITERAL.search(line):
                continue
            # the only allowed literals are last-resort fallbacks after a token lookup in script: getPropertyValue(...) || '#abc'
            if rel.endswith(".js") and re.search(r"\|\|\s*'#[0-9a-fA-F]{3,8}'", COLOUR_LITERAL.sub(lambda m: m.group(0), line)) \
                    and all(re.search(r"\|\|\s*'#[0-9a-fA-F]{3,8}'", seg) for seg in re.findall(r"[^;]*#[0-9a-fA-F]{3,8}[^;]*", line)):
                continue
            bad.append("%s:%d" % (rel, no))
    assert not bad, bad


def test_every_token_in_use_is_defined():
    base_light, _ = _base()
    defined = set(base_light)
    used = set()
    for rel in _component_files():
        used |= set(re.findall(r"var\((--ll-[\w-]*\w)(?![\w-])", read(*rel.split("/"))))
    # tokens with a local fallback or set by a component itself
    local = {k for k in used if k not in defined and re.search(r"var\(%s\s*," % re.escape(k), "".join(read(*r.split("/")) for r in _component_files()))}
    assert used - defined - local == set(), used - defined - local
