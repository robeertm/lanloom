"""Visible text in the web files goes through the translation function t('key'), never a literal.

The scan looks at the places where the app puts text in front of a person: the children and the title / placeholder / alt /
aria-label attributes of h('tag', {...}, ...) calls, element properties (textContent, title, ...), setAttribute of those
attributes, HTML strings, and the message helpers. A literal with letters there fails the test unless it is listed in ALLOWED below
with a reason (like the brand check). The scan is a safety net with a few simple rules, not a parser: when a rule trips on
something that is not text, add the exact literal to ALLOWED with the reason; when it misses a pattern, add the pattern here.

Also checked here: numbers, dates and times go through Intl (no hand-made decimal points, no 12-hour clock), and a plural is one
key with plural forms, never a pair of keys picked with `n === 1`."""
import os
import re

import pytest

from _jsscan import call_args, tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "lanloom", "web")

# (file name, literal text) -> reason. Every entry has to be needed; a stale entry fails the test.
ALLOWED = {
    ("index.html", "Lanloom"): "the product name in <title>; a proper name is not translated",
    ("index.html", "Lanloom needs JavaScript."): "<noscript> shows exactly when no script runs, so it cannot be translated by one; the page title and the text are English by necessity",
    ("app.js", "Lanloom"): "the product name; a proper name is not translated",
    ("app.js", "Lanloom "): "the product name; a proper name is not translated",
    ("views.js", "Lanloom"): "the product name; a proper name is not translated",
    ("views.js", " · Lanloom"): "separator and the product name in the page title; a proper name is not translated",
    ("wizard.js", "Lanloom"): "the product name; a proper name is not translated",
    ("wizard.js", "ABCD-EFGH"): "an example of the shape of the setup code, not a word",
    ("backup.js", "Lanloom"): "the product name; a proper name is not translated",
    ("backup.js", "ABCD-EFGH"): "an example of the shape of the setup code, not a word",
    ("wizard.js", 'docker logs lanloom 2>&1 | grep -i "setup code"'): "a shell command the reader copies; commands are not translated",
    ("ai.js", "sh install.sh --with-ai"): "a shell command the reader copies; commands are not translated",
    ("firewall.js", "VLAN "): "VLAN is the standard term in every language",
    ("firewall.js", "G"): "unit prefix on a port tile (2.5G), the same in every language",
    ("firewall.js", " Mbit/s"): "unit symbol, the same in every language",
    ("palette.js", "Esc"): "the label printed on the Escape key of a keyboard; the same in every language",
    ("firewall.js", "Passkey"): "the platform's own word for the credential; the same in every language",
}

TEXT_PROPS = {"title", "placeholder", "alt", "label", "text", "textContent", "innerText", "ariaLabel", "aria-label", "aria-description",
              "aria-roledescription", "aria-valuetext", "aria-placeholder", "tooltip", "caption", "heading", "hint"}
TEXT_ASSIGN = {"textContent", "innerText", "title", "placeholder", "alt", "ariaLabel", "label"}
TEXT_ATTRS = {"title", "placeholder", "alt", "aria-label", "aria-description", "aria-roledescription", "aria-valuetext", "aria-placeholder"}
COMPARE = {"===", "!==", "==", "!=", "<", ">", "<=", ">="}
VALUE_PREV = {"?", ":", "||", "&&", "??", "+", "(", ",", "["}
LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)
KEYLIKE = re.compile(r"^[a-z][A-Za-z0-9_]*(\.[A-Za-z0-9_\-]+)+\.?$")


def files():
    out = []
    for sub in ("js", "board"):
        d = os.path.join(WEB, sub)
        out += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".js")]
    out.append(os.path.join(WEB, "index.html"))
    return out


def has_letters(s):
    return bool(LETTERS.search(s))


def texty(s):
    """A literal that reads like a phrase: more than one word, or a capitalised word. One lower-case word is a value, not text."""
    s = s.strip()
    return has_letters(s) and (bool(re.search(r"\s", s)) or bool(re.match(r"[A-ZÄÖÜ][a-zäöüß]{2,}", s)))


def strip_builtin_strings(src):
    """The built-in English defaults of the board modules sit between strings:begin and strings:end (checked against en by test_board_i18n)."""
    a, b = src.find("/* strings:begin */"), src.find("/* strings:end */")
    return src if a < 0 or b < 0 else src[:a] + src[b:]


def html_text(s):
    """Visible text of an HTML string: text nodes with letters and the text attributes. \\0 marks a ${...} hole."""
    found = []
    for m in re.finditer(r">([^<>]+)(?=<)", s):
        piece = m.group(1).replace("\0", "")
        if has_letters(piece):
            found.append(piece.strip())
    for m in re.finditer(r"\b(title|placeholder|alt|aria-label)=\"([^\"]*)\"", s):
        piece = m.group(2).replace("\0", "")
        if has_letters(piece):
            found.append(piece.strip())
    return found


def scan_js(path):
    """[(line, literal, rule)] of visible literals in one JS file."""
    with open(path, encoding="utf-8") as f:
        return scan_source(f.read())


def scan_source(src):
    """The same for JS source text (the scanner's own tests plant literals without touching the source tree)."""
    src = strip_builtin_strings(src)
    toks = tokenize(src)
    hits = []

    def literal_values(a, b, top_only):
        """String literals of toks[a:b] in value position. top_only: only a literal that is the whole operand (any letters count);
        otherwise only phrase-like literals (more than one word, capitalised) deeper inside."""
        out, depth_call = [], []
        k = a
        while k < b:
            t = toks[k]
            if t.kind == "ident" and t.val in ("h", "svgEl") and k + 1 < b and toks[k + 1].val == "(" and toks[k - 1].val != ".":
                k = call_args(toks, k + 1)[1] + 1      # a nested h() call is scanned on its own
                continue
            if t.kind == "punct" and t.val == "(":
                is_call = k > a and toks[k - 1].kind == "ident" or (k > a and toks[k - 1].kind == "punct" and toks[k - 1].val in (")", "]"))
                depth_call.append(is_call)
            elif t.kind == "punct" and t.val == "[":
                depth_call.append(True)                  # an array literal is data ('a', 'b').map(...), not shown as it is
            elif t.kind == "punct" and t.val in (")", "]"):
                if depth_call:
                    depth_call.pop()
            elif t.kind == "str" and has_letters(t.val):
                prev = toks[k - 1] if k > a else None
                nxt = toks[k + 1] if k + 1 < b else None
                p = prev.val if prev is not None and prev.kind == "punct" else None
                in_call = any(depth_call)
                callee = toks[k - 2].val if prev is not None and p == "(" and k - 2 >= a and toks[k - 2].kind == "ident" else None
                comparison = (p in COMPARE) or (nxt is not None and nxt.kind == "punct" and nxt.val in COMPARE)
                is_key_arg = callee in ("t", "key") or KEYLIKE.match(t.val)
                if comparison or is_key_arg:
                    pass
                elif prev is None or p in VALUE_PREV:
                    whole = not in_call
                    if (top_only and whole) or texty(t.val):
                        # a word used as a function argument ('x'.join, includes('x'), ...) is not shown as it is
                        if not in_call or texty(t.val):
                            out.append(t)
            elif t.kind == "tpl":
                for piece in html_text(t.val):
                    out.append(Tok_like(t, piece))
            k += 1
        return out

    i = 0
    while i < len(toks):
        t = toks[i]
        # h('tag', {props}, ...children) and svgEl(...)
        if t.kind == "ident" and t.val in ("h", "svgEl") and i + 1 < len(toks) and toks[i + 1].val == "(" and (i == 0 or toks[i - 1].val != "."):
            args, end = call_args(toks, i + 1)
            for idx, (a, b) in enumerate(args):
                if idx == 1:
                    # props object: key: value pairs with a text key
                    k = a
                    while k < b:
                        key = toks[k]
                        if key.kind in ("ident", "str") and k + 1 < b and toks[k + 1].val == ":" and key.val in TEXT_PROPS:
                            e = k + 2
                            depth = 0
                            while e < b and not (depth == 0 and toks[e].val == ","):
                                depth += (toks[e].val in ("(", "[", "{")) - (toks[e].val in (")", "]", "}"))
                                e += 1
                            for lit in literal_values(k + 2, e, True):
                                hits.append((lit.line, lit.val, "attribute " + key.val))
                            k = e
                        k += 1
                elif idx >= 2:
                    for lit in literal_values(a, b, True):
                        hits.append((lit.line, lit.val, "child of h()"))
        # element.textContent = '...'
        if t.kind == "punct" and t.val == "." and i + 2 < len(toks) and toks[i + 1].val in TEXT_ASSIGN and toks[i + 2].val == "=":
            e = i + 3
            depth = 0
            while e < len(toks) and not (depth == 0 and toks[e].val in (";", ",")) and depth >= 0:
                depth += (toks[e].val in ("(", "[", "{")) - (toks[e].val in (")", "]", "}"))
                e += 1
            for lit in literal_values(i + 3, e, True):
                hits.append((lit.line, lit.val, "." + toks[i + 1].val + " ="))
        # el.setAttribute('aria-label', '...')
        if t.kind == "ident" and t.val == "setAttribute" and i + 1 < len(toks) and toks[i + 1].val == "(":
            args, _ = call_args(toks, i + 1)
            if len(args) == 2 and toks[args[0][0]].kind == "str" and toks[args[0][0]].val in TEXT_ATTRS:
                for lit in literal_values(args[1][0], args[1][1], True):
                    hits.append((lit.line, lit.val, "setAttribute " + toks[args[0][0]].val))
        # message helpers: LL.msg(el, 'text', kind), alert / confirm / prompt
        if t.kind == "ident" and t.val in ("msg", "alert", "confirm", "prompt", "toast") and i + 1 < len(toks) and toks[i + 1].val == "(":
            args, _ = call_args(toks, i + 1)
            at = 1 if t.val == "msg" and len(args) > 1 else 0
            if args and len(args) > at:
                for lit in literal_values(args[at][0], args[at][1], True):
                    hits.append((lit.line, lit.val, t.val + "()"))
        # HTML strings / templates anywhere
        if t.kind in ("str", "tpl") and "<" in t.val and t.kind == "tpl" or (t.kind == "str" and re.search(r"<\w", t.val)):
            for piece in html_text(t.val):
                hits.append((t.line, piece, "HTML text"))
        i += 1
    return sorted(set(hits))


class Tok_like:
    def __init__(self, t, val):
        self.line, self.val = t.line, val


def scan_html(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    hits = []
    for m in re.finditer(r"<(title|noscript)[^>]*>(.*?)</\1>", src, re.S):
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if has_letters(text):
            hits.append((src.count("\n", 0, m.start()) + 1, text, "<%s>" % m.group(1)))
    for m in re.finditer(r"\b(?:alt|placeholder|aria-label)=\"([^\"]*)\"", src):
        if has_letters(m.group(1)):
            hits.append((src.count("\n", 0, m.start()) + 1, m.group(1), "attribute"))
    return hits


def all_hits():
    out = []
    for path in files():
        name = os.path.basename(path)
        for line, lit, rule in (scan_html(path) if name.endswith(".html") else scan_js(path)):
            out.append((name, line, lit, rule))
    return out


def test_visible_text_goes_through_t():
    bad, used = [], set()
    for name, line, lit, rule in all_hits():
        if (name, lit) in ALLOWED:
            used.add((name, lit))
            continue
        bad.append("%s:%d  %r  (%s)" % (name, line, lit, rule))
    assert not bad, "visible text without a translation key (use t('key') and add the text to every language, or list it in ALLOWED with a reason):\n" + "\n".join(bad)
    stale = sorted(set(ALLOWED) - used)
    assert not stale, "ALLOWED entries that are not needed any more: %s" % stale


def test_scanner_finds_what_it_should():
    """The scan itself: a planted literal in every supported place is found, calls through t() are not."""
    planted = """
const x = h('div', {}, 'Hello there');
const y = h('button', { title: 'Save changes', 'aria-label': 'Close' }, t('ok.key'));
el.textContent = 'Nothing here';
node.setAttribute('aria-label', 'Open menu');
LL.msg(box, 'It did not work', 'error');
const z = h('p', {}, cond ? 'Shown text' : t('other.key'));
box.innerHTML = '<b>Bold text</b>';
const f = `<span>Template text ${n}</span>`;
"""
    got = {lit for _, lit, _ in scan_source(planted)}
    for lit in ("Hello there", "Save changes", "Close", "Nothing here", "Open menu", "It did not work", "Shown text", "Bold text", "Template text"):
        assert lit in got, (lit, sorted(got))
    assert not ({"ok.key", "other.key", "error"} & got)


def test_scanner_ignores_what_is_not_text():
    clean = """
const a = h('div', { class: 'card big', id: 'x', 'data-k': 'Some Value' }, t('a.b'), String(n), items.join(', '), kind === 'router' ? t('r') : t('s'));
const b = h('span', { class: 'muted' }, '·', ' ', '—', n + ' %');
el.className = 'on';
const c = ['alpha', 'beta'].includes(v) ? 1 : 2;
const d = /Some Words/.test(s) ? 1 : 2;
"""
    got = scan_source(clean)
    assert not got, got


# ------------------------------------------------------------------ numbers, dates, times, plurals
# `.toFixed(` writes a decimal point; for text a person reads use LL.fmtNum (Intl). Remaining uses are SVG / CSS geometry (always a point).
TOFIXED_GEOMETRY = {
    "paths.js": (21, "SVG path and style values of the world map arcs, zoom viewBox, label transforms and radii"),
    "hypervisor.js": (2, "SVG path of the sparkline"),
    "endmarks.js": (8, "CSS transform and SVG line coordinates"),
    "textfmt.js": (1, "fallback of the number formatter when Intl is missing"),
    "board.js": (1, "rounding of board coordinates (num helper)"),
    "automations.js": (1, "CSS left/width percentages of the day plan"),
}


def test_numbers_go_through_intl():
    for path in files():
        name = os.path.basename(path)
        if not name.endswith(".js"):
            continue
        with open(path, encoding="utf-8") as f:
            src = f.read()
        count = len(re.findall(r"\.toFixed\(", src))
        allowed = TOFIXED_GEOMETRY.get(name, (0, ""))[0]
        assert count == allowed, "%s: %d uses of .toFixed (decimal point, not localised); use LL.fmtNum for text, or raise the geometry allowance with a reason" % (name, count)
        assert not re.search(r"toLocale\w*String\(\s*\)", src), name + ": toLocale*String() without a locale"
        assert not re.search(r"hour12\s*:\s*true", src), name + ": 24-hour clock only"
        assert not re.search(r"toLocale\w*String\(\s*undefined", src), name + ": pass the page locale (LL.locale())"


def test_plural_is_one_key_not_a_pair():
    for path in files():
        name = os.path.basename(path)
        if not name.endswith(".js"):
            continue
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert not re.search(r"\bt\(\s*[\w.\[\]]+\s*===\s*1\s*\?", src), name + ": pick the plural form with {n, plural, one {...} other {...}} inside one key"
        assert not re.search(r"===\s*1\s*\?\s*t\(\s*'[^']*(_one|\.one|_many|\.many|_single|_plural)'", src), name + ": pick the plural form with {n, plural, one {...} other {...}} inside one key"


def test_pages_use_the_page_locale():
    for path in files():
        name = os.path.basename(path)
        if not name.endswith(".js"):
            continue
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert not re.search(r"LL\.lang\s*===?\s*'de'\s*\?\s*'de-DE'", src), name + ": use LL.locale()"
