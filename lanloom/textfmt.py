"""Text and number formatting for translated texts rendered on the server (nightly report deliveries).

The web app does the same in ``web/js/textfmt.js`` (``Intl``); ``tests/test_i18n_format.py`` keeps both in step.
Translated texts may carry plural forms in a small ICU subset: ``{n, plural, one {# device} other {# devices}}``;
``=0`` style exact matches win over the category, ``#`` is the number written the way the language writes it.
"""
import re
from pathlib import Path

I18N = Path(__file__).resolve().parent / "i18n"

# CLDR plural rule per language (cardinals, integers): the category for the number n
def _one_other(n: int) -> str:
    return "one" if n == 1 else "other"


PLURAL_RULES = {"en": _one_other, "de": _one_other}
# categories every translation of a plural text has to carry, per language (CLDR)
PLURAL_CATEGORIES = {"en": ("one", "other"), "de": ("one", "other")}
# decimal separator and grouping separator per language
SEPARATORS = {"en": (".", ","), "de": (",", ".")}

_HEAD = re.compile(r"\{(\w+),\s*plural,\s*")
_CASE = re.compile(r"(=\d+|[a-z]+)\s*\{")


def languages() -> list:
    """Languages with a translation file: ``<lang>.json`` plus ``board.<lang>.json`` in lanloom/i18n (a new language is just two files)."""
    return sorted(p.stem for p in I18N.glob("*.json") if not p.name.startswith("board."))


def category(lang: str, n: int) -> str:
    return PLURAL_RULES.get(lang, _one_other)(n)


def num(lang: str, n, digits=None) -> str:
    """A number as the language writes it; ``digits`` fixes the fraction digits, otherwise up to two."""
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "–"
    if v != v or v in (float("inf"), float("-inf")):
        return "–"
    dec, grp = SEPARATORS.get(lang, SEPARATORS["en"])
    text = ("%.*f" % (digits, v)) if digits is not None else ("%.2f" % v).rstrip("0").rstrip(".")
    whole, _, frac = text.partition(".")
    sign = "-" if whole.startswith("-") else ""
    whole = whole.lstrip("-")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return sign + grp.join(groups) + (dec + frac if frac else "")


def _construct(s: str, j: int):
    """The plural construct starting at s[j] == "{": (variable, {category: text}, index after it), or None."""
    head = _HEAD.match(s, j)
    if not head:
        return None
    p = head.end()
    branch = {}
    while p < len(s) and s[p] != "}":
        c = _CASE.match(s, p)
        if not c:
            return None
        p = c.end()
        depth, q = 1, p
        while q < len(s) and depth:
            depth += (s[q] == "{") - (s[q] == "}")
            q += 1
        branch[c.group(1)] = s[p:q - 1]
        p = q
        while p < len(s) and s[p].isspace():
            p += 1
    return head.group(1), branch, p + 1


def _plurals(s: str, vars_: dict, lang: str) -> str:
    out, i = [], 0
    while True:
        j = s.find("{", i)
        if j < 0:
            out.append(s[i:])
            return "".join(out)
        found = _construct(s, j)
        if not found:
            out.append(s[i:j + 1])
            i = j + 1
            continue
        out.append(s[i:j])
        name, branch, i = found
        raw = (vars_ or {}).get(name)
        if raw is None:
            out.append(branch.get("other", "").replace("#", "{%s}" % name))
            continue
        n = float(raw)
        n_int = int(n) if n == int(n) else n
        text = branch.get("=%s" % n_int)
        if text is None:
            text = branch.get(category(lang, n_int), branch.get("other", ""))
        out.append(text.replace("#", num(lang, n)))


def fill(text: str, vars_: dict = None, lang: str = "en") -> str:
    text = str(text)
    if ", plural," in text:
        text = _plurals(text, vars_, lang)
    if not vars_:
        return text
    return re.sub(r"\{(\w+)\}", lambda m: str(vars_[m.group(1)]) if vars_.get(m.group(1)) is not None else m.group(0), text)


def constructs(text: str) -> list:
    """Every plural construct of a text as (variable, {category: branch text}), outermost first."""
    out, i = [], 0
    while True:
        j = text.find("{", i)
        if j < 0:
            return out
        found = _construct(text, j)
        if not found:
            i = j + 1
            continue
        out.append((found[0], found[1]))
        for body in found[1].values():
            out.extend(constructs(body))
        i = found[2]


def placeholders(text: str) -> set:
    """Variable names a text expects (plural variables included, branch words are not); the texts of one key must agree."""
    names, i = set(), 0
    while True:
        j = text.find("{", i)
        if j < 0:
            return names
        found = _construct(text, j)
        if found:
            names.add(found[0])
            for body in found[1].values():
                names |= placeholders(body)
            i = found[2]
            continue
        m = re.compile(r"\{(\w+)\}").match(text, j)
        if m:
            names.add(m.group(1))
            i = m.end()
        else:
            i = j + 1
