"""Plain-text rendering of a report for deliveries (the web app renders the same keys itself)."""
import json
import re
from datetime import date, datetime
from pathlib import Path

I18N = Path(__file__).resolve().parent.parent / "i18n"
SUFFIXES = ("_at", "_dur", "_bytes", "_cc", "_key")
WEEKDAYS = {"en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), "de": ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")}
MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_cache: dict = {}


def strings(lang: str) -> dict:
    lang = lang if lang in ("en", "de") else "en"
    if lang not in _cache:
        try:
            _cache[lang] = json.loads((I18N / ("%s.json" % lang)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache[lang] = {}
    return _cache[lang]


def fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit, f in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= f:
            v = n / f
            return ("%.0f" % v if v >= 10 else "%.1f" % v) + " " + unit
    return "%d B" % round(n)


def fmt_dur(s, tr) -> str:
    s = max(0, int(s or 0))
    if s < 60:
        return tr("report.dur.s", n=s)
    if s < 3600:
        return tr("report.dur.m", n=round(s / 60))
    if s < 86400:
        return tr("report.dur.hm", h=s // 3600, m=(s % 3600) // 60) if (s % 3600) // 60 else tr("report.dur.h", h=s // 3600)
    return tr("report.dur.dh", d=s // 86400, h=(s % 86400) // 3600)


def fmt_date(day: str, lang: str) -> str:
    d = date.fromisoformat(day)
    return "%02d.%02d.%d" % (d.day, d.month, d.year) if lang == "de" else "%d %s %d" % (d.day, MONTHS_EN[d.month - 1], d.year)


def fmt_time(t: float, tz, day: str, lang: str) -> str:
    dt = datetime.fromtimestamp(float(t), tz)
    hm = "%02d:%02d" % (dt.hour, dt.minute)
    if dt.date().isoformat() == day:
        return hm
    wd = WEEKDAYS[lang][dt.weekday()]
    return ("%s %02d.%02d. " % (wd, dt.day, dt.month) if lang == "de" else "%s %d %s " % (wd, dt.day, MONTHS_EN[dt.month - 1])) + hm


class Renderer:
    def __init__(self, lang: str, tz, day: str):
        self.lang = lang if lang in ("en", "de") else "en"
        self.s = strings(self.lang)
        self.tz, self.day = tz, day

    def tr(self, key: str, **vars_) -> str:
        text = self.s.get(key, key)
        return re.sub(r"\{(\w+)\}", lambda m: str(vars_[m.group(1)]) if m.group(1) in vars_ else m.group(0), text)

    def values(self, vars_: dict) -> dict:
        out = {}
        for k, v in (vars_ or {}).items():
            base = next((k[:-len(s)] for s in SUFFIXES if k.endswith(s)), k)
            if k.endswith("_at"):
                out[base] = fmt_time(v, self.tz, self.day, self.lang) if v else "–"
            elif k.endswith("_dur"):
                out[base] = fmt_dur(v, self.tr)
            elif k.endswith("_bytes"):
                out[base] = fmt_bytes(v)
            elif k.endswith("_key"):
                out[base] = self.tr(str(v))
            else:
                out[base] = v
        return out

    def line(self, entry: dict) -> str:
        text = self.tr(entry["key"], **self.values(entry.get("vars") or {}))
        parts = [self.line(p) for p in entry.get("parts") or []]
        return " · ".join([text] + parts)


def headline(report: dict, r: Renderer) -> str:
    n = report.get("points", 0)
    if not n:
        return r.tr("report.head.calm")
    return r.tr("report.head.one") if n == 1 else r.tr("report.head.many", n=n)


def short_text(report: dict, lang: str, tz, link: str) -> dict:
    r = Renderer(lang, tz, report["day"])
    title = r.tr("report.mail.title", date=fmt_date(report["day"], r.lang))
    head = headline(report, r)
    points = [(s, i) for s in report["sections"] for i in s["items"] if i["point"]]
    lines = []
    for sec, it in points[:2]:
        lines.append("%s: %s" % (r.tr(sec["title"]), r.line(it)))
    if len(points) > 2:
        lines[-1] += " " + r.tr("report.mail.more", n=len(points) - 2)
    if not lines:
        for sec in report["sections"][:2]:
            lines.append("%s: %s" % (r.tr(sec["title"]), r.line(sec["summary"])))
    url = ("%s/#/reports/%s" % (link, report["day"])) if link else ""
    text = "\n".join([head] + lines[:2] + ([url] if url else []))
    return {"title": title, "headline": head, "lines": lines[:2], "text": text, "url": url, "points": report.get("points", 0),
            "day": report["day"]}


def test_text(lang: str, link: str) -> dict:
    r = Renderer(lang, None, "")
    lines = [r.tr("report.mail.test_line")]
    text = "\n".join([r.tr("report.mail.test_head")] + lines + ([link] if link else []))
    return {"title": r.tr("report.mail.test_title"), "headline": r.tr("report.mail.test_head"), "lines": lines, "text": text,
            "url": link or "", "points": 0, "day": ""}
