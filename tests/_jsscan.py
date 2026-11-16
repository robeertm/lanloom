"""A small JavaScript tokenizer for the language tests (strings, template literals, comments and regular expressions are told apart;
no parsing). Used by test_i18n_literals.py to find visible text that does not go through the translation function."""
import re

PUNCT3 = ("===", "!==", "...", "**=", "<<=", ">>=", "&&=", "||=", "??=", ">>>")
PUNCT2 = ("=>", "==", "!=", "<=", ">=", "&&", "||", "??", "?.", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<", ">>", "**")
REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^") | {"return", "typeof", "case", "in", "of", "else", "do", "void", "delete", "throw", "new", "yield", "await"}


class Tok:
    __slots__ = ("kind", "val", "pos", "line")

    def __init__(self, kind, val, pos, line):
        self.kind, self.val, self.pos, self.line = kind, val, pos, line

    def __repr__(self):
        return "Tok(%s, %r, line %d)" % (self.kind, self.val, self.line)


def tokenize(src: str):
    """Tokens: ident, num, str (value without quotes, escapes resolved for the common ones), tpl (static text of a template literal;
    the ${...} expressions follow as ordinary tokens), re, punct. Comments are dropped."""
    toks, i, n, line = [], 0, len(src), 1

    def prev_allows_regex():
        if not toks:
            return True
        p = toks[-1]
        if p.kind in ("num", "str", "tpl", "re"):
            return False
        if p.kind == "ident":
            return p.val in REGEX_AFTER
        return p.val in REGEX_AFTER or p.val in ("&&", "||", "??", "=>", "==", "===", "!=", "!==")

    def read_string(q, i):
        out, i = [], i + 1
        while i < n and src[i] != q:
            if src[i] == "\\" and i + 1 < n:
                nxt = src[i + 1]
                out.append({"n": "\n", "t": "\t", "u": "\\u"}.get(nxt, nxt))
                i += 2
                if nxt == "u":
                    m = re.match(r"[0-9a-fA-F]{4}|\{[0-9a-fA-F]+\}", src[i:i + 8])
                    if m:
                        code = m.group(0).strip("{}")
                        out[-1] = chr(int(code, 16))
                        i += len(m.group(0))
                continue
            out.append(src[i])
            i += 1
        return "".join(out), i + 1

    def read_template(i, depth_stack):
        """Reads a template literal starting at the backtick; returns (static text, index after, [(expr_start, expr_end)])."""
        static, exprs, i = [], [], i + 1
        while i < n and src[i] != "`":
            if src[i] == "\\" and i + 1 < n:
                static.append(src[i + 1])
                i += 2
                continue
            if src[i] == "$" and src[i + 1:i + 2] == "{":
                depth, j = 1, i + 2
                while j < n and depth:
                    c = src[j]
                    if c in "'\"":
                        _, j = read_string(c, j)
                        continue
                    if c == "`":
                        _, j, _ = read_template(j, depth_stack)
                        continue
                    depth += (c == "{") - (c == "}")
                    j += 1
                exprs.append((i + 2, j - 1))
                static.append("\0")
                i = j
                continue
            static.append(src[i])
            i += 1
        return "".join(static), i + 1, exprs

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
        elif c.isspace():
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
        elif c in "'\"":
            val, j = read_string(c, i)
            toks.append(Tok("str", val, i, line))
            line += src.count("\n", i, j)
            i = j
        elif c == "`":
            val, j, exprs = read_template(i, [])
            toks.append(Tok("tpl", val, i, line))
            for a, b in exprs:
                for t in tokenize(src[a:b]):
                    toks.append(Tok(t.kind, t.val, a + t.pos, line + t.line - 1))
            line += src.count("\n", i, j)
            i = j
        elif c == "/" and prev_allows_regex():
            j, in_class = i + 1, False
            while j < n and (src[j] != "/" or in_class) and src[j] != "\n":
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            toks.append(Tok("re", src[i:j], i, line))
            i = j
        elif c.isalpha() or c in "_$":
            m = re.compile(r"[A-Za-z0-9_$]+").match(src, i)
            toks.append(Tok("ident", m.group(0), i, line))
            i = m.end()
        elif c.isdigit():
            m = re.compile(r"[0-9][0-9a-fA-FxXeE_.]*").match(src, i)
            toks.append(Tok("num", m.group(0), i, line))
            i = m.end()
        else:
            for p in PUNCT3 + PUNCT2:
                if src.startswith(p, i):
                    toks.append(Tok("punct", p, i, line))
                    i += len(p)
                    break
            else:
                toks.append(Tok("punct", c, i, line))
                i += 1
    return toks


def call_args(toks, open_idx):
    """For the "(" at toks[open_idx]: ([(start, end) token index ranges of the top-level arguments], index of the closing ")")."""
    depth, args, start, i = 0, [], open_idx + 1, open_idx
    while i < len(toks):
        t = toks[i]
        if t.kind == "punct":
            if t.val in "([{":
                depth += 1
            elif t.val in ")]}":
                depth -= 1
                if depth == 0:
                    if start < i:
                        args.append((start, i))
                    return args, i
            elif t.val == "," and depth == 1:
                args.append((start, i))
                start = i + 1
        i += 1
    return args, len(toks) - 1
