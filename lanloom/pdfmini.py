"""A very small PDF writer for the exports (network plan and inventory list): vector shapes and text, nothing else.

No dependency is added to the image for this: the file is PDF 1.4 with the two standard fonts Helvetica and
Helvetica-Bold (they are part of every PDF reader, so nothing is embedded), text as real text (searchable, copyable),
shapes as paths. Texts are written in the Western European 8-bit code page cp1252 (English and German are covered; a character outside it
becomes ``?``).

Coordinates of the drawing calls are *top-down* like on a screen, in the units of the current view (``view(ox, oy, scale)``
maps them onto the page); a page is ``Page(width_pt, height_pt)``.
"""
from __future__ import annotations

import zlib
from typing import Iterable, Optional, Sequence

# Helvetica advance widths (1/1000 em) of the characters 32..255 in cp1252 (the standard AFM metrics).
_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556,
      278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722,
      667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500,
      222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 761, 556, 761, 222, 556, 333,
      1000, 556, 556, 333, 1000, 667, 333, 1000, 761, 611, 761, 761, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944,
      761, 500, 667, 278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 556,
      537, 278, 333, 333, 365, 556, 834, 834, 834, 611, 667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278,
      278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 500,
      556, 556, 556, 556, 278, 278, 278, 278, 556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500]
BOLD_FACTOR = 1.07            # Helvetica-Bold is about 7 % wider than Helvetica for ordinary text
_SPECIAL = {"→": "->", "←": "<-", "·": "·", "—": "—", "–": "–", "✓": "+", "×": "x", " ": " ", " ": " ", " ": " "}


def clean(text) -> str:
    """Text as the cp1252 subset the fonts carry (a control character becomes a space)."""
    out = []
    for ch in str(text if text is not None else ""):
        ch = _SPECIAL.get(ch, ch)
        for c in ch:
            if c < " " or c == "\x7f":
                out.append(" ")
                continue
            try:
                c.encode("cp1252")
                out.append(c)
            except UnicodeEncodeError:
                out.append("?")
    return "".join(out)


def text_width(text: str, size: float, bold: bool = False) -> float:
    total = 0
    for c in clean(text):
        try:
            b = c.encode("cp1252")[0]
        except UnicodeEncodeError:
            b = 63
        total += _W[b - 32] if 32 <= b <= 255 else 556
    return total * size / 1000.0 * (BOLD_FACTOR if bold else 1.0)


def fit(text: str, size: float, max_w: float, bold: bool = False) -> str:
    """The text cut with an ellipsis so that it fits into ``max_w``."""
    text = clean(text)
    if text_width(text, size, bold) <= max_w:
        return text
    while text and text_width(text + "…", size, bold) > max_w:
        text = text[:-1]
    return (text.rstrip() + "…") if text else ""


def _rgb(color: str) -> tuple:
    c = (color or "#000000").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return tuple(int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return (0.0, 0.0, 0.0)


def _n(v: float) -> str:
    s = ("%.2f" % v).rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _esc(b: bytes) -> bytes:
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class Page:
    def __init__(self, width: float, height: float):
        self.w, self.h = float(width), float(height)
        self.ops: list = []
        self.ox = self.oy = 0.0
        self.s = 1.0

    # ------------------------------------------------------------------ view
    def view(self, ox: float = 0.0, oy: float = 0.0, scale: float = 1.0) -> None:
        """Drawing units -> page: x_page = ox + scale * x, y measured from the top: y_page = oy + scale * y."""
        self.ox, self.oy, self.s = ox, oy, scale

    def _x(self, x: float) -> float:
        return self.ox + self.s * x

    def _y(self, y: float) -> float:
        return self.h - (self.oy + self.s * y)

    # ------------------------------------------------------------------ state
    def clip(self, x: float, y: float, w: float, h: float) -> None:
        """Everything until ``restore()`` is cut to this rectangle (view units)."""
        self.ops.append("q %s %s %s %s re W n" % (_n(self._x(x)), _n(self._y(y + h)), _n(w * self.s), _n(h * self.s)))

    def restore(self) -> None:
        self.ops.append("Q")

    def _paint(self, fill: Optional[str], stroke: Optional[str], lw: float, dash: Optional[Sequence[float]] = None, close: bool = False) -> None:
        ops = []
        if fill:
            ops.append("%s %s %s rg" % tuple(_n(v) for v in _rgb(fill)))
        if stroke:
            ops.append("%s %s %s RG %s w" % (tuple(_n(v) for v in _rgb(stroke)) + (_n(lw * self.s),)))
            ops.append("[%s] 0 d" % " ".join(_n(d * self.s) for d in dash) if dash else "[] 0 d")
        op = ("b" if close else "B") if fill and stroke else ("f" if fill else ("s" if close else "S") if stroke else "n")
        ops.append(op)
        self.ops.append("\n".join(ops))

    # ------------------------------------------------------------------ shapes
    def rect(self, x, y, w, h, fill=None, stroke=None, lw=1.0, radius=0.0, dash=None) -> None:
        X, Y, W, H = self._x(x), self._y(y + h), w * self.s, h * self.s
        r = min(radius * self.s, W / 2, H / 2)
        if r <= 0.01:
            path = "%s %s %s %s re" % (_n(X), _n(Y), _n(W), _n(H))
        else:
            k = r * 0.5523
            path = (f"{_n(X + r)} {_n(Y)} m {_n(X + W - r)} {_n(Y)} l {_n(X + W - r + k)} {_n(Y)} {_n(X + W)} {_n(Y + r - k)} {_n(X + W)} {_n(Y + r)} c "
                    f"{_n(X + W)} {_n(Y + H - r)} l {_n(X + W)} {_n(Y + H - r + k)} {_n(X + W - r + k)} {_n(Y + H)} {_n(X + W - r)} {_n(Y + H)} c "
                    f"{_n(X + r)} {_n(Y + H)} l {_n(X + r - k)} {_n(Y + H)} {_n(X)} {_n(Y + H - r + k)} {_n(X)} {_n(Y + H - r)} c "
                    f"{_n(X)} {_n(Y + r)} l {_n(X)} {_n(Y + r - k)} {_n(X + r - k)} {_n(Y)} {_n(X + r)} {_n(Y)} c h")
        self.ops.append(path)
        self._paint(fill, stroke, lw, dash)

    def circle(self, cx, cy, r, fill=None, stroke=None, lw=1.0) -> None:
        X, Y, R = self._x(cx), self._y(cy), r * self.s
        k = R * 0.5523
        self.ops.append(f"{_n(X + R)} {_n(Y)} m {_n(X + R)} {_n(Y + k)} {_n(X + k)} {_n(Y + R)} {_n(X)} {_n(Y + R)} c "
                        f"{_n(X - k)} {_n(Y + R)} {_n(X - R)} {_n(Y + k)} {_n(X - R)} {_n(Y)} c "
                        f"{_n(X - R)} {_n(Y - k)} {_n(X - k)} {_n(Y - R)} {_n(X)} {_n(Y - R)} c "
                        f"{_n(X + k)} {_n(Y - R)} {_n(X + R)} {_n(Y - k)} {_n(X + R)} {_n(Y)} c h")
        self._paint(fill, stroke, lw)

    def polyline(self, pts: Iterable[tuple], stroke: str, lw: float = 1.0, dash=None) -> None:
        pts = list(pts)
        if len(pts) < 2:
            return
        parts = ["%s %s m" % (_n(self._x(pts[0][0])), _n(self._y(pts[0][1])))]
        parts += ["%s %s l" % (_n(self._x(x)), _n(self._y(y))) for x, y in pts[1:]]
        parts.append("1 j 1 J")
        self.ops.append(" ".join(parts))
        self._paint(None, stroke, lw, dash)

    def text(self, x, y, s, size=10.0, color="#000000", bold=False, anchor="start") -> None:
        """One line of text; ``y`` is the baseline. ``anchor``: start, middle or end."""
        s = clean(s)
        if not s:
            return
        size_p = size * self.s
        width = text_width(s, size_p, bold)
        X = self._x(x) - (width / 2 if anchor == "middle" else width if anchor == "end" else 0)
        raw = _esc(s.encode("cp1252", "replace")).decode("latin-1")
        self.ops.append("BT /%s %s Tf %s %s %s rg %s %s Td (%s) Tj ET" % (
            "F2" if bold else "F1", _n(size_p), *(_n(v) for v in _rgb(color)), _n(X), _n(self._y(y)), raw))

    def content(self) -> bytes:
        return ("\n".join(self.ops) + "\n").encode("latin-1")


class Pdf:
    def __init__(self, title: str = "", producer: str = "Lanloom", created: str = ""):
        self.pages: list = []
        self.title, self.producer, self.created = clean(title), clean(producer), created

    def page(self, width: float, height: float) -> Page:
        p = Page(width, height)
        self.pages.append(p)
        return p

    def render(self) -> bytes:
        objs: list = []                         # object number n is objs[n - 1]

        def add(data: bytes) -> int:
            objs.append(data)
            return len(objs)

        catalog = add(b"")
        pages = add(b"")
        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        kids = []
        for p in self.pages:
            stream = zlib.compress(p.content(), 6)
            cont = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream")
            kids.append(add(("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] /Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> /Contents %d 0 R >>"
                             % (pages, _n(p.w), _n(p.h), f1, f2, cont)).encode()))
        objs[pages - 1] = ("<< /Type /Pages /Count %d /Kids [%s] >>" % (len(kids), " ".join("%d 0 R" % k for k in kids))).encode()
        objs[catalog - 1] = ("<< /Type /Catalog /Pages %d 0 R >>" % pages).encode()
        info = "<< /Title (%s) /Producer (%s)%s >>" % (
            _esc(self.title.encode("cp1252", "replace")).decode("latin-1"), _esc(self.producer.encode("cp1252", "replace")).decode("latin-1"),
            (" /CreationDate (D:%s)" % self.created) if self.created else "")
        info_n = add(info.encode("latin-1"))
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, data in enumerate(objs, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i + data + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        for off in offsets:
            out += b"%010d 00000 n \n" % off
        out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalog, info_n, xref)
        return bytes(out)
