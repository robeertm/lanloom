"""No direct DOM call in the web app may receive a child that can be null (lanloom#12).

`LL.h()` skips null and false children, so `cond ? h(...) : null` inside `h(...)` is fine. The DOM methods
`replaceChildren`, `append`, `prepend`, `before`, `after` and `replaceWith` do not: they turn null into the text "null"
(and false into "false") and put it on the page. This check reads every file under lanloom/web/js and flags a
top-level argument of such a call that
  - is a conditional whose result can be null (`a ? b : null`, `a ? null : b`) or a short-circuit (`a && b`),
  - is a `const` of the same file whose value is such an expression, or
  - calls a function of the same file that can return null (`return null`, a bare `return`, `return a ? b : null`),
unless the call filters its children (`.filter(Boolean)`) or is guarded by the same name (`if (x) el.append(x)`).
"""
import pathlib
import re
import sys

JS = pathlib.Path(__file__).resolve().parent.parent / "lanloom" / "web" / "js"
CALL = re.compile(r"\.(replaceChildren|append|prepend|before|after|replaceWith)\(")


class _Nullish:
    """Can this top-level expression be null or false? A conditional by its branches, otherwise null itself or `a && b`."""
    @staticmethod
    def search(flat):
        if "?" in flat:
            return re.search(r"[?:]\s*null\s*(?::|$)", flat)
        return flat == "null" or ("&&" in flat and "||" not in flat.split("&&")[-1])


NULLISH = _Nullish()


def _strip(src):
    """Comments and string contents blanked (same length, line numbers stay), so brackets in text do not count."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", src[i:j])); i = j
        elif c in "'\"`":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append(c + re.sub(r"[^\n]", " ", src[i + 1:j]) + c); i = j + 1
        elif c == "/" and re.search(r"[(,=:!&|?;{}\[]\s*$|return\s*$|^\s*$", src[max(0, i - 20):i]):    # a regex literal
            j = i + 1
            while j < n and src[j] not in "/\n":
                if src[j] == "[":
                    while j < n and src[j] != "]":
                        j += 2 if src[j] == "\\" else 1
                j += 2 if src[j] == "\\" else 1
            out.append("/" + " " * (j - i - 1)); i = j
        else:
            out.append(c); i += 1
    return "".join(out)


def _close(s, i):
    """Index just after the bracket that closes the one opened right before i."""
    depth = 1
    while i < len(s) and depth:
        depth += s[i] in "([{"
        depth -= s[i] in ")]}"
        i += 1
    return i


def _flat(expr):
    """Everything inside brackets removed: what is left is the top level of the expression."""
    out, depth = [], 0
    for c in expr:
        if c in "([{":
            if depth == 0:
                out.append(c)
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                out.append(c)
        elif depth == 0:
            out.append(c)
    return "".join(out).strip()


def _args(s):
    parts, depth, cur = [], 0, []
    for c in s:
        depth += c in "([{"
        depth -= c in ")]}"
        if c == "," and depth == 0:
            parts.append("".join(cur).strip()); cur = []
        else:
            cur.append(c)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _can_return_null(s, name):
    m = (re.search(r"\bfunction\s+%s\s*\([^)]*\)\s*\{" % re.escape(name), s)
         or re.search(r"\b(?:const|let)\s+%s\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>\s*\{" % re.escape(name), s))
    if not m:
        return False
    body = s[m.end():_close(s, m.end()) - 1]
    for inner in sorted(re.finditer(r"=>\s*\{|\bfunction\b[^{;]*\{", body), key=lambda x: -x.start()):   # nested functions do not count
        end = _close(body, inner.end())
        body = body[:inner.end()] + " " * (end - 1 - inner.end()) + body[end - 1:]
    for r in re.finditer(r"\breturn\b", body):
        rest = body[r.end():]
        expr = rest[:re.search(r";|\n\s*\}|$", rest).start()]
        if not expr.strip() or NULLISH.search(_flat(expr)):
            return True
    return False


def _findings():
    bad = []
    for p in sorted(JS.glob("*.js")):
        s = _strip(p.read_text(encoding="utf-8"))
        consts = {}
        for m in re.finditer(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*", s):
            rest = s[m.end():]
            i, depth = 0, 0
            while i < len(rest) and not (depth == 0 and rest[i] in ";\n,"):
                depth += rest[i] in "([{"
                depth -= rest[i] in ")]}"
                if depth < 0:
                    break
                i += 1
            consts[m.group(1)] = _flat(rest[:i])
        for m in CALL.finditer(s):
            end = _close(s, m.end())
            line = s.count("\n", 0, m.start()) + 1
            if ".filter(Boolean)" in s[m.end():end + 20]:
                continue
            for a in _args(s[m.end():end - 1]):
                a = a[3:] if a.startswith("...") else a
                flat = _flat(a)
                if NULLISH.search(flat):
                    why = "conditional child"
                elif re.fullmatch(r"[A-Za-z_$][\w$]*", a) and a in consts and NULLISH.search(consts[a]):
                    why = "const that can be null"
                elif re.fullmatch(r"[A-Za-z_$][\w$]*\(\)", flat) and _can_return_null(s, flat[:-2]):
                    why = "function that can return null"
                else:
                    continue
                head = s[s.rfind("\n", 0, m.start()) + 1:m.start()]
                name = re.match(r"[\w$]+", a).group(0) if re.match(r"[\w$]+", a) else ""
                if name and re.search(r"\bif\s*\(\s*%s\s*\)|\b%s\s*&&" % (re.escape(name), re.escape(name)), head):
                    continue
                bad.append("%s:%d %s(%s) - %s" % (p.name, line, m.group(1), a[:50], why))
    return bad


def test_no_nullable_child_reaches_a_dom_call():
    bad = _findings()
    assert not bad, "a null child turns into the text \"null\" on the page; filter the children (.filter(Boolean)):\n" + "\n".join(bad)


def test_the_check_sees_the_patterns(tmp_path, monkeypatch):
    """The check itself: each pattern of lanloom#12 is caught, the safe forms are not."""
    (tmp_path / "x.js").write_text("\n".join([
        "function note(d) { if (!d) return null; return h('p', {}, 'x'); }",
        "function safe(d) { const f = () => { return null; }; return h('p', {}, f); }",
        "const card = LL.x ? LL.x() : null;",
        "body.replaceChildren(h('div', {}, a ? b : null), card, tail);",            # 4: const
        "body.replaceChildren(note(d), tail);",                                      # 5: function
        "box.append(h('p'), d.why ? h('p', {}, d.why) : null);",                    # 6: conditional
        "box.append(ok && h('p'));",                                                 # 7: short-circuit
        "body.replaceChildren(...[note(d), card].filter(Boolean));",                 # filtered
        "if (card) page.append(card);",                                              # guarded
        "body.replaceChildren(safe(d), h('p', {}, x ? y : null));",                 # null only inside h / nested fn
        "// box.append(a ? b : null) in a comment",
        "box.append(h('p', {}, 'a ? b : null'));",                                  # in a string
    ]), encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "JS", tmp_path)
    got = sorted(int(x.split(":")[1].split()[0]) for x in _findings())
    assert got == [4, 5, 6, 7], got
