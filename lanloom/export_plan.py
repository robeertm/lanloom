"""The network plan as a document: SVG (vector, texts as text), draw.io (editable boxes, zones and lines) and PDF (plan plus
inventory list, A4 or A3).

One *scene* is laid out from the map model (the same one the web page draws) and written by three small writers, so the three
files show the same plan. The layout is the board's idea in a fixed grid: zones are boards, every device is a pad with its name,
address and state, lines run only in the gaps between pads (like traces on a circuit board), never across a pad. Three looks:
``light``, ``dark`` and ``print`` (white paper, no fills, state also written in words, so a grey-scale printout is complete).

Everything that is said on the page goes through ``tr`` (the translated texts), the ``redact`` option replaces names and addresses
by the pseudonyms of export.Redactor.
"""
from __future__ import annotations

import html
import math
import re
from typing import Callable, Optional

from . import pdfmini
from .pdfmini import fit, text_width

PAD_W, PAD_H, GAP_X, GAP_Y = 176.0, 46.0, 16.0, 12.0
ZONE_PAD, ZONE_HEAD, MARGIN, ROW_GAP = 14.0, 28.0, 28.0, 46.0
PLAN_W = 1240.0                      # width of the board area; rows of zones wrap at this width
HEAD_H = 54.0
LEGEND_H = 46.0
KIND_ORDER = ["internet", "router", "switch", "ap", "self", "nas", "server", "hub", "broker", "service", "container", "computer", "phone",
              "tv", "media", "camera", "iot", "printer", "unknown"]
LOOKS = ("light", "dark", "print")
PAGES = {"A4": (841.89, 595.28), "A3": (1190.55, 841.89)}      # landscape, in points

PALETTES = {
    "light": {"bg": "#f4f6f8", "ink": "#16202a", "muted": "#5b6875", "zone": "#ffffff", "zone_line": "#c5ced8", "pad": "#ffffff", "pad_line": "#8997a6",
              "trace": "#4a7fa8", "trace2": "#9aa7b4", "up": "#1f9d57", "down": "#d03b3b", "unknown": "#8a96a3", "accent": "#2f6fa3"},
    "dark": {"bg": "#10161c", "ink": "#e8eef4", "muted": "#9aa8b6", "zone": "#18212a", "zone_line": "#2f3d4a", "pad": "#1f2a35", "pad_line": "#4a5b6b",
             "trace": "#58a0d0", "trace2": "#5f7183", "up": "#3ecf82", "down": "#ff6b6b", "unknown": "#7d8a97", "accent": "#6bb3e6"},
    "print": {"bg": "#ffffff", "ink": "#000000", "muted": "#444444", "zone": "#ffffff", "zone_line": "#777777", "pad": "#ffffff", "pad_line": "#222222",
              "trace": "#111111", "trace2": "#666666", "up": "#000000", "down": "#000000", "unknown": "#888888", "accent": "#000000"},
}
SOLID = ("uplink", "lan", "cable", "wired")
THICK = ("uplink",)


def _kind_rank(kind: str) -> int:
    return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)


# ---------------------------------------------------------------------------------------------------- the scene
def _cols(n: int) -> int:
    return 1 if n <= 3 else 2 if n <= 8 else 3 if n <= 18 else 4


def build_scene(model: dict, tr: Callable, rd=None, title: str = "", sub: str = "") -> dict:
    """Pads, zones and lines with coordinates. ``tr(key, vars)`` translates; ``rd`` is an export.Redactor or None."""
    zones_in = {z["id"]: z for z in model.get("zones", [])}
    comps = [c for c in model.get("components", []) if not c.get("quiet_more")]
    by_zone: dict = {}
    for c in comps:
        z = c.get("zone") if c.get("zone") in zones_in else "other"
        by_zone.setdefault(z, []).append(c)
    if "other" in by_zone and "other" not in zones_in:
        zones_in["other"] = {"id": "other", "title": "", "title_key": "zone.other", "order": 99, "kind": "other"}

    def comp_title(c):
        if rd:
            return rd.name(c["id"], c.get("kind")) if c["id"] != "internet" else tr("kind.internet")
        if c.get("title"):
            return c["title"]
        return tr(c["title_key"], c.get("vars") or {}) if c.get("title_key") else tr("kind." + str(c.get("kind")))

    def comp_sub(c):
        if rd:
            return rd.ip(c.get("subtitle", "")) if re.match(r"^\d+\.\d+\.\d+\.\d+$", str(c.get("subtitle") or "")) else (
                tr(c["subtitle_key"], c.get("vars") or {}) if c.get("subtitle_key") else "")
        if c.get("subtitle"):
            return c["subtitle"]
        return tr(c["subtitle_key"], c.get("vars") or {}) if c.get("subtitle_key") else ""

    def zone_title(z):
        v = dict(z.get("vars") or {})
        if rd:
            if z.get("kind") == "net":
                return rd.net(z.get("title") or "")
            if "host" in v:
                v["host"] = rd.name(z.get("fold") or "host", "host")
            if z.get("title") and not z.get("title_key"):
                return rd.text(z["title"]) or tr("zone.other")
        if z.get("title_key"):
            return tr(z["title_key"], v)
        return z.get("title") or ""

    order = sorted((zid for zid in by_zone), key=lambda zid: (zones_in[zid].get("order", 50), zid))
    full = [zid for zid in order if zones_in[zid].get("span") == "full"]
    rest = [zid for zid in order if zid not in full]
    zones, pads, pad_of, bands = [], [], {}, []
    y = MARGIN + HEAD_H

    def lay_block(zid, x0, y0, width_hint=None):
        cs = sorted(by_zone[zid], key=lambda c: (_kind_rank(c.get("kind")), str(comp_title(c)).lower()))
        full_span = zones_in[zid].get("span") == "full"
        cols = min(len(cs), int((PLAN_W - 2 * ZONE_PAD + GAP_X) // (PAD_W + GAP_X))) if full_span else _cols(len(cs))
        rows_n = math.ceil(len(cs) / cols)
        w = cols * PAD_W + (cols - 1) * GAP_X + 2 * ZONE_PAD
        h = ZONE_HEAD + rows_n * PAD_H + (rows_n - 1) * GAP_Y + ZONE_PAD
        blk = {"id": zid, "title": zone_title(zones_in[zid]), "kind": zones_in[zid].get("kind") or "", "x": x0, "y": y0, "w": w, "h": h}
        zones.append(blk)
        for i, c in enumerate(cs):
            r, k = divmod(i, cols)
            p = {"id": c["id"], "x": x0 + ZONE_PAD + k * (PAD_W + GAP_X), "y": y0 + ZONE_HEAD + r * (PAD_H + GAP_Y), "w": PAD_W, "h": PAD_H,
                 "title": comp_title(c), "sub": comp_sub(c), "kind": c.get("kind") or "unknown",
                 "kind_label": tr("kind." + str(c.get("kind") or "unknown")) or str(c.get("kind") or ""), "status": c.get("status") or "unknown", "zone": zid, "col": k, "block": blk}
            pads.append(p)
            pad_of[c["id"]] = p
        return blk

    for zid in full:
        blk = lay_block(zid, MARGIN, y)
        bands.append((y, y + blk["h"]))
        y += blk["h"] + ROW_GAP
    x, row_h = MARGIN, 0.0
    first = True
    for zid in rest:
        cs = by_zone[zid]
        cols = _cols(len(cs))
        w = cols * PAD_W + (cols - 1) * GAP_X + 2 * ZONE_PAD
        if not first and x + w > MARGIN + PLAN_W:
            bands.append((y, y + row_h))
            x, y, row_h = MARGIN, y + row_h + ROW_GAP, 0.0
        blk = lay_block(zid, x, y)
        x += blk["w"] + GAP_X * 1.5
        row_h = max(row_h, blk["h"])
        first = False
    if row_h:
        bands.append((y, y + row_h))
    y += row_h
    width = max([MARGIN * 2 + PLAN_W] + [b["x"] + b["w"] + MARGIN for b in zones])
    edges = _route(model.get("traces", []), pad_of, bands)
    height = y + MARGIN + LEGEND_H
    return {"w": width, "h": height, "zones": zones, "pads": pads, "edges": edges, "title": title, "sub": sub, "legend_y": y + 14, "bands": bands}


def _route(traces: list, pad_of: dict, bands: Optional[list] = None) -> list:
    """Orthogonal lines that cross no pad. Neighbours in one row: straight through the gap. One zone: through its column and row gaps
    (``_route_in_zone``). Zones in one row of the plan: out of A into the column gap beside it, along the channel above the row, in at B.
    Zones in different rows: the vertical parts stay inside A's and B's own zones (their column gaps hold no pad, and below a zone that is
    shorter than its row there is nothing); between the two rows the line runs along the row gap, and when other rows lie between them,
    down or up the left margin of the plan, which no zone reaches. ``bands`` are the (top, bottom) of the plan's rows."""
    bands = list(bands or [])

    def band_of(blk):
        for i, (top, bottom) in enumerate(bands):
            if top - 1 <= blk["y"] and blk["y"] + blk["h"] <= bottom + 1:
                return i
        return None

    lanes: dict = {}

    def lane(key, n):
        lanes[key] = lanes.get(key, -1) + 1
        return lanes[key] % n

    def gap_y(i):                                                # the row gap below row i (each line in it a little lower than the last)
        return bands[i][1] + 9 + lane(("gap", i), 7) * 4.0

    out = []
    for t in traces:
        a, b = pad_of.get(t["a"]), pad_of.get(t["b"])
        if not a or not b or a is b:
            continue
        kind = str(t.get("kind") or "lan")
        A, B = (a, b)
        ab, bb = A["block"], B["block"]
        ia, ib = band_of(ab), band_of(bb)
        if ab is bb and abs(A["y"] - B["y"]) < 1 and abs(abs(A["x"] - B["x"]) - (PAD_W + GAP_X)) < 1:
            lo, hi = (A, B) if A["x"] < B["x"] else (B, A)        # neighbours in one row: a straight line through the gap between them
            pts = [(lo["x"] + lo["w"], lo["y"] + lo["h"] / 2), (hi["x"], hi["y"] + hi["h"] / 2)]
        elif ab is bb:
            pts = _route_in_zone(A, B)
        else:
            rx = A["x"] + A["w"] + GAP_X / 2                     # A's column gap (or zone padding) on its right
            lx = B["x"] - GAP_X / 2                              # B's column gap (or zone padding) on its left
            ay, by_ = A["y"] + A["h"] / 2, B["y"] + B["h"] / 2
            if ia is None or ib is None or ia == ib:             # same row of the plan: along the channel above it
                ch = bb["y"] - 9 - lane(id(bb), 7) * 4.0
                pts = [(A["x"] + A["w"], ay), (rx, ay), (rx, ch), (lx, ch), (lx, by_), (B["x"], by_)]
            else:
                ga = gap_y(ia - 1) if ib < ia else gap_y(ia)     # the row gap on A's side towards B
                gb = gap_y(ib) if ib < ia else gap_y(ib - 1)     # the row gap on B's side towards A
                if abs(ia - ib) == 1:                             # neighbouring rows: one gap between them
                    pts = [(A["x"] + A["w"], ay), (rx, ay), (rx, ga), (lx, ga), (lx, by_), (B["x"], by_)]
                else:                                             # rows between them: down or up the left margin
                    cx = MARGIN - 8 - lane("margin", 4) * 4.0
                    pts = [(A["x"] + A["w"], ay), (rx, ay), (rx, ga), (cx, ga), (cx, gb), (lx, gb), (lx, by_), (B["x"], by_)]
        out.append({"id": t["id"], "a": A["id"], "b": B["id"], "kind": kind, "pts": pts, "solid": kind in SOLID, "thick": kind in THICK})
    return out


def _route_in_zone(A: dict, B: dict) -> list:
    """Two pads of one zone that are not neighbours in a row: out of A into the column gap beside it, along the row gap next to B's row,
    down or up the column gap beside B and in at its side. Pads of a zone sit on one grid, so the column and row gaps (and the zone padding
    around the outer pads) hold no pad - the line never crosses one, also when other pads stand between A and B in the same row."""
    ay, by_ = A["y"] + A["h"] / 2, B["y"] + B["h"] / 2
    if B["x"] < A["x"] - 1:                                       # B further left: leave A on the left, enter B on its right
        ax, rx = A["x"], A["x"] - GAP_X / 2
        bx, lx = B["x"] + B["w"], B["x"] + B["w"] + GAP_X / 2
    elif B["x"] > A["x"] + 1:                                     # B further right: leave A on the right, enter B on its left
        ax, rx = A["x"] + A["w"], A["x"] + A["w"] + GAP_X / 2
        bx, lx = B["x"], B["x"] - GAP_X / 2
    else:                                                         # same column: both on the right side
        ax, rx = A["x"] + A["w"], A["x"] + A["w"] + GAP_X / 2
        bx, lx = B["x"] + B["w"], rx
    if B["y"] > A["y"] + 1:                                       # B in a lower row: the row gap just above B's row
        cy = B["y"] - GAP_Y / 2
    else:                                                         # B in a higher or the same row: the row gap (or zone padding) below B's row
        cy = B["y"] + B["h"] + GAP_Y / 2
    pts = [(ax, ay), (rx, ay), (rx, cy), (lx, cy), (lx, by_), (bx, by_)]
    return [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]


# ---------------------------------------------------------------------------------------------------- SVG
def _x(s) -> str:
    return html.escape(str(s), quote=True)


def _f(v: float) -> str:
    return ("%.1f" % v).rstrip("0").rstrip(".")


def _status_word(status: str, tr) -> str:
    return tr("export.plan.state." + status) if status in ("down",) else ""


def _legend_items(pal: dict, tr) -> list:
    return [("line", tr("export.plan.legend.cable"), True, False), ("line", tr("export.plan.legend.other"), False, False),
            ("dot", tr("export.plan.legend.up"), "up", None), ("dot", tr("export.plan.legend.down"), "down", None),
            ("dot", tr("export.plan.legend.unknown"), "unknown", None)]


def to_svg(scene: dict, look: str, tr: Callable) -> str:
    pal = PALETTES.get(look, PALETTES["light"])
    W, H = scene["w"], scene["h"]
    o = ['<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %s %s" width="%s" height="%s" '
         'font-family="Helvetica, Arial, sans-serif" role="img" aria-label="%s">' % (_f(W), _f(H), _f(W), _f(H), _x(scene["title"]))]
    o.append("<title>%s</title>" % _x(scene["title"]))
    o.append('<rect width="%s" height="%s" fill="%s"/>' % (_f(W), _f(H), pal["bg"]))
    o.append('<text x="%s" y="%s" font-size="18" font-weight="700" fill="%s">%s</text>' % (_f(MARGIN), _f(MARGIN + 18), pal["ink"], _x(scene["title"])))
    o.append('<text x="%s" y="%s" font-size="11" fill="%s">%s</text>' % (_f(MARGIN), _f(MARGIN + 36), pal["muted"], _x(scene["sub"])))
    for z in scene["zones"]:
        o.append('<g><rect x="%s" y="%s" width="%s" height="%s" rx="9" fill="%s" stroke="%s" stroke-width="1"/>' % (
            _f(z["x"]), _f(z["y"]), _f(z["w"]), _f(z["h"]), pal["zone"] if look != "print" else "none", pal["zone_line"]))
        o.append('<text x="%s" y="%s" font-size="12" font-weight="700" fill="%s">%s</text></g>' % (
            _f(z["x"] + ZONE_PAD), _f(z["y"] + 19), pal["muted"], _x(fit(z["title"], 12, z["w"] - 2 * ZONE_PAD, True))))
    for e in scene["edges"]:
        d = " ".join(("M" if i == 0 else "L") + "%s %s" % (_f(px), _f(py)) for i, (px, py) in enumerate(e["pts"]))
        o.append('<path d="%s" fill="none" stroke="%s" stroke-width="%s"%s stroke-linejoin="round" stroke-linecap="round"></path>' % (
            d, pal["trace"] if e["solid"] else pal["trace2"], "2.4" if e["thick"] else "1.4", "" if e["solid"] else ' stroke-dasharray="5 4"'))
    for p in scene["pads"]:
        o.append('<g><title>%s</title><rect x="%s" y="%s" width="%s" height="%s" rx="7" fill="%s" stroke="%s" stroke-width="1.2"/>' % (
            _x(p["title"] + (" · " + p["sub"] if p["sub"] else "")), _f(p["x"]), _f(p["y"]), _f(p["w"]), _f(p["h"]), pal["pad"], pal["pad_line"]))
        col = pal.get(p["status"], pal["unknown"])
        hollow = look == "print" and p["status"] != "up"
        o.append('<circle cx="%s" cy="%s" r="4.2" fill="%s" stroke="%s" stroke-width="1.2"/>' % (
            _f(p["x"] + p["w"] - 11), _f(p["y"] + 12), "none" if hollow else col, col))
        o.append('<text x="%s" y="%s" font-size="8.5" fill="%s" letter-spacing="0.4">%s</text>' % (
            _f(p["x"] + 10), _f(p["y"] + 13), pal["muted"], _x(fit(str(p["kind_label"]).upper(), 8.5, p["w"] - 36))))
        o.append('<text x="%s" y="%s" font-size="12" font-weight="700" fill="%s">%s</text>' % (
            _f(p["x"] + 10), _f(p["y"] + 28), pal["ink"], _x(fit(p["title"], 12, p["w"] - 20, True))))
        word = _status_word(p["status"], tr)
        subtext = (p["sub"] + (" · " + word if word and p["sub"] else word)) if (p["sub"] or word) else ""
        o.append('<text x="%s" y="%s" font-size="10" fill="%s">%s</text></g>' % (
            _f(p["x"] + 10), _f(p["y"] + 41), pal["down"] if p["status"] == "down" and look != "print" else pal["muted"], _x(fit(subtext, 10, p["w"] - 20))))
    lx = MARGIN
    ly = scene["legend_y"] + 12
    for kind, label, a, b in _legend_items(pal, tr):
        if kind == "line":
            o.append('<path d="M%s %sL%s %s" stroke="%s" stroke-width="2" fill="none"%s/>' % (
                _f(lx), _f(ly), _f(lx + 26), _f(ly), pal["trace"] if a else pal["trace2"], "" if a else ' stroke-dasharray="5 4"'))
            lx += 34
        else:
            col = pal[a]
            o.append('<circle cx="%s" cy="%s" r="4.2" fill="%s" stroke="%s" stroke-width="1.2"/>' % (
                _f(lx + 4), _f(ly), "none" if (look == "print" and a != "up") else col, col))
            lx += 14
        o.append('<text x="%s" y="%s" font-size="10.5" fill="%s">%s</text>' % (_f(lx), _f(ly + 3.5), pal["muted"], _x(label)))
        lx += text_width(label, 10.5) + 22
    o.append("</svg>\n")
    return "\n".join(o)


# ---------------------------------------------------------------------------------------------------- draw.io
def to_drawio(scene: dict, look: str, tr: Callable) -> str:
    pal = PALETTES.get(look, PALETTES["light"])
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    ids: dict = {}

    def cell_id(raw: str) -> str:
        return ids.setdefault(raw, "c%d" % (len(ids) + 2))

    def attr(v) -> str:
        return html.escape(str(v), quote=True)

    for i, z in enumerate(scene["zones"]):
        style = ("rounded=1;arcSize=3;whiteSpace=wrap;html=1;verticalAlign=top;align=left;spacingLeft=10;spacingTop=2;fontStyle=1;fontSize=12;"
                 "fillColor=%s;strokeColor=%s;fontColor=%s;" % ("none" if look == "print" else pal["zone"], pal["zone_line"], pal["muted"]))
        cells.append('<mxCell id="z%d" value="%s" style="%s" vertex="1" parent="1"><mxGeometry x="%s" y="%s" width="%s" height="%s" as="geometry"/></mxCell>'
                     % (i, attr(z["title"]), style, _f(z["x"]), _f(z["y"]), _f(z["w"]), _f(z["h"])))
    for p in scene["pads"]:
        word = _status_word(p["status"], tr)
        sub = (p["sub"] + (" · " + word if word and p["sub"] else word)) if (p["sub"] or word) else ""
        value = "<font style=\"font-size:8px\" color=\"%s\">%s</font><br><b>%s</b><br><font style=\"font-size:10px\" color=\"%s\">%s</font>" % (
            pal["muted"], html.escape(str(p["kind_label"]).upper()), html.escape(p["title"]), pal["muted"], html.escape(sub))
        edge_col = pal.get(p["status"], pal["unknown"]) if p["status"] == "down" else pal["pad_line"]
        style = ("rounded=1;arcSize=14;whiteSpace=wrap;html=1;align=left;spacingLeft=8;fillColor=%s;strokeColor=%s;fontColor=%s;fontSize=12;strokeWidth=%s;"
                 % (pal["pad"], edge_col, pal["ink"], "2" if p["status"] == "down" else "1"))
        cells.append('<mxCell id="%s" value="%s" style="%s" vertex="1" parent="1"><mxGeometry x="%s" y="%s" width="%s" height="%s" as="geometry"/></mxCell>'
                     % (cell_id(p["id"]), attr(value), style, _f(p["x"]), _f(p["y"]), _f(p["w"]), _f(p["h"])))
    for i, e in enumerate(scene["edges"]):
        style = "endArrow=none;html=1;rounded=0;edgeStyle=orthogonalEdgeStyle;strokeColor=%s;strokeWidth=%s;%s" % (
            pal["trace"] if e["solid"] else pal["trace2"], "3" if e["thick"] else "1.5", "" if e["solid"] else "dashed=1;")
        cells.append('<mxCell id="e%d" style="%s" edge="1" parent="1" source="%s" target="%s"><mxGeometry relative="1" as="geometry"/></mxCell>'
                     % (i, style, cell_id(e["a"]), cell_id(e["b"])))
    page_w, page_h = int(scene["w"]), int(scene["h"])
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<mxfile host="Lanloom" agent="Lanloom"><diagram id="lanloom-plan" name="%s">'
            '<mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
            'pageWidth="%d" pageHeight="%d" background="%s" math="0" shadow="0"><root>\n%s\n</root></mxGraphModel></diagram></mxfile>\n'
            % (attr(scene["title"]), page_w, page_h, pal["bg"] if look != "print" else "#ffffff", "\n".join(cells)))


# ---------------------------------------------------------------------------------------------------- PDF
def draw_scene(page: pdfmini.Page, scene: dict, look: str, tr: Callable, header: bool = True) -> None:
    """The scene in the page's current view (scene units)."""
    pal = PALETTES.get(look, PALETTES["light"])
    W, H = scene["w"], scene["h"]
    if look != "print":
        page.rect(0, 0, W, H, fill=pal["bg"])
    if header:
        page.text(MARGIN, MARGIN + 18, scene["title"], 18, pal["ink"], True)
        page.text(MARGIN, MARGIN + 36, scene["sub"], 11, pal["muted"])
    for z in scene["zones"]:
        page.rect(z["x"], z["y"], z["w"], z["h"], fill=None if look == "print" else pal["zone"], stroke=pal["zone_line"], lw=1, radius=9)
        page.text(z["x"] + ZONE_PAD, z["y"] + 19, fit(z["title"], 12, z["w"] - 2 * ZONE_PAD, True), 12, pal["muted"], True)
    for e in scene["edges"]:
        page.polyline(e["pts"], pal["trace"] if e["solid"] else pal["trace2"], 2.4 if e["thick"] else 1.4, None if e["solid"] else (5, 4))
    for p in scene["pads"]:
        page.rect(p["x"], p["y"], p["w"], p["h"], fill=pal["pad"], stroke=pal["pad_line"], lw=1.2, radius=7)
        col = pal.get(p["status"], pal["unknown"])
        hollow = look == "print" and p["status"] != "up"
        page.circle(p["x"] + p["w"] - 11, p["y"] + 12, 4.2, fill=None if hollow else col, stroke=col, lw=1.2)
        page.text(p["x"] + 10, p["y"] + 13, fit(str(p["kind_label"]).upper(), 8.5, p["w"] - 36), 8.5, pal["muted"])
        page.text(p["x"] + 10, p["y"] + 28, fit(p["title"], 12, p["w"] - 20, True), 12, pal["ink"], True)
        word = _status_word(p["status"], tr)
        subtext = (p["sub"] + (" · " + word if word and p["sub"] else word)) if (p["sub"] or word) else ""
        page.text(p["x"] + 10, p["y"] + 41, fit(subtext, 10, p["w"] - 20), 10, pal["down"] if p["status"] == "down" and look != "print" else pal["muted"])
    lx, ly = MARGIN, scene["legend_y"] + 12
    for kind, label, a, b in _legend_items(pal, tr):
        if kind == "line":
            page.polyline([(lx, ly), (lx + 26, ly)], pal["trace"] if a else pal["trace2"], 2, None if a else (5, 4))
            lx += 34
        else:
            col = pal[a]
            page.circle(lx + 4, ly, 4.2, fill=None if (look == "print" and a != "up") else col, stroke=col, lw=1.2)
            lx += 14
        page.text(lx, ly + 3.5, label, 10.5, pal["muted"])
        lx += text_width(label, 10.5) + 22


def _breaks(scene: dict, cap: float) -> list:
    """Scene heights at which pages start: between rows of zones where possible (cap = scene height one page holds)."""
    bands = list(scene.get("bands") or [])
    if not bands:
        return [0.0]
    bands[-1] = (bands[-1][0], scene["h"])
    starts, cur = [0.0], 0.0
    for top, bot in bands:
        if bot - cur > cap and top - ROW_GAP + 4 > cur + 40:        # this row does not fit any more: a new page begins above it
            cur = top - ROW_GAP + 4
            starts.append(cur)
        while bot - cur > cap:                                      # a single zone row taller than a page: cut it
            cur += cap
            starts.append(cur)
    return starts


def plan_pages(pdf: pdfmini.Pdf, scene: dict, look: str, size: str, tr: Callable) -> int:
    """The scene on the pages of the chosen size: one page when it stays readable (text of at least about 6 pt), else cut between
    rows of zones. Returns the number of pages added."""
    pw, ph = PAGES.get(size, PAGES["A4"])
    m, foot = 22.0, 18.0
    avail_w, avail_h = pw - 2 * m, ph - 2 * m - foot
    s = min(avail_w / scene["w"], 1.25)
    s_one = min(s, avail_h / scene["h"])
    if s_one >= 0.52:
        s, starts, cap = s_one, [0.0], scene["h"]
    else:
        cap = avail_h / s
        starts = _breaks(scene, cap)
    pal = PALETTES.get(look, PALETTES["light"])
    ends = starts[1:] + [scene["h"]]
    for i, (top, end) in enumerate(zip(starts, ends)):
        pg = pdf.page(pw, ph)
        if look != "print":
            pg.rect(0, 0, pw, ph, fill=pal["bg"])
        pg.clip(m, m, avail_w, min(avail_h, (end - top) * s + 2))      # only what belongs to this page: the next row starts on the next one
        pg.view(m, m - top * s, s)
        draw_scene(pg, scene, look, tr, header=(i == 0))
        pg.restore()
        pg.view()
    return len(starts)


# ---------------------------------------------------------------------------------------------------- the inventory list in the PDF
LIST_COLUMNS = (("name", "export.col.name", 1.5), ("kind", "export.col.kind", 1.0), ("ip", "export.col.ip", 1.05), ("mac", "export.col.mac", 1.3),
                ("network", "export.col.network", 1.15), ("room", "export.col.room", 0.9), ("purpose", "export.col.purpose", 1.5),
                ("owner", "export.col.owner", 0.8), ("connected_via", "export.col.via", 1.3), ("port", "export.col.port", 0.5),
                ("state", "export.col.state", 0.8), ("warranty_until", "export.col.warranty", 0.95), ("eol_until", "export.col.eol", 0.95))


def inventory_pages(pdf: pdfmini.Pdf, data: list, look: str, size: str, tr: Callable, title: str) -> int:
    pw, ph = PAGES.get(size, PAGES["A4"])
    pal = PALETTES.get(look, PALETTES["light"])
    m, foot, fs, rh = 22.0, 18.0, 7.6, 15.0
    weights = sum(c[2] for c in LIST_COLUMNS)
    avail_w = pw - 2 * m - 6
    xs, acc = [], m + 3
    for _, _, w in LIST_COLUMNS:
        xs.append(acc)
        acc += avail_w * w / weights
    widths = [avail_w * c[2] / weights - 5 for c in LIST_COLUMNS]
    head_h = 34.0
    per_first = int((ph - 2 * m - foot - head_h - 22) // rh)
    per_page = int((ph - 2 * m - foot - 22) // rh)

    def cell(r, key):
        v = r.get(key, "")
        if key == "kind":
            return tr("kind." + str(v)) or str(v)
        if key == "state":
            return tr("inventory.state." + str(v)) or str(v)
        return str(v or "")

    pages, i = 0, 0
    rows = list(data)
    while True:
        pg = pdf.page(pw, ph)
        pages += 1
        if look != "print":
            pg.rect(0, 0, pw, ph, fill=pal["bg"])
        top = m
        if pages == 1:
            pg.text(m, m + 14, title, 15, pal["ink"], True)
            pg.text(m, m + 28, tr("export.inventory.count", {"n": len(rows)}), 9, pal["muted"])
            top = m + head_h
        pg.rect(m, top, pw - 2 * m, 17, fill=pal["zone"] if look != "print" else None, stroke=pal["zone_line"], lw=0.8, radius=3)
        for (key, lab, _), x, w in zip(LIST_COLUMNS, xs, widths):
            pg.text(x, top + 12, fit(tr(lab), fs, w, True), fs, pal["ink"], True)
        y = top + 17
        cap = per_first if pages == 1 else per_page
        for r in rows[i:i + cap]:
            for (key, _, _w), x, w in zip(LIST_COLUMNS, xs, widths):
                pg.text(x, y + 10.5, fit(cell(r, key), fs, w), fs, pal["down"] if (key == "state" and r.get("state") in ("missing", "removed") and look != "print") else pal["ink"])
            y += rh
            pg.polyline([(m, y), (pw - m, y)], pal["zone_line"], 0.4)
        i += cap
        if i >= len(rows):
            break
    return pages


def footers(pdf: pdfmini.Pdf, look: str, footer: str) -> None:
    """Footer line and page numbers on every page (after all pages exist, so the total is known)."""
    pal = PALETTES.get(look, PALETTES["light"])
    n = len(pdf.pages)
    for i, pg in enumerate(pdf.pages, 1):
        pg.view()
        pg.text(22.0, pg.h - 22.0 + 4, footer, 8, pal["muted"])
        pg.text(pg.w - 22.0, pg.h - 22.0 + 4, "%d / %d" % (i, n), 8, pal["muted"], anchor="end")
