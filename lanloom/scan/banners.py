"""Web banners: page <title> and `Server` header of HTTP(S) ports (GET / only)."""
from __future__ import annotations

import html
import re

from .httpget import http_get

BANNER_CAP = 32 * 1024
MAX_TEXT = 160
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_CHARSET_RE = re.compile(r"charset=([A-Za-z0-9_\-]+)", re.IGNORECASE)


def clean_text(text, limit: int = MAX_TEXT) -> str | None:
    """Collapse whitespace, drop control characters, cap length. None when empty."""
    if text is None:
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    text = " ".join(text.split())
    if not text:
        return None
    return text[:limit]


def extract_title(body: bytes, content_type: str | None = None) -> str | None:
    if not body:
        return None
    m = _TITLE_RE.search(body[:BANNER_CAP])
    if not m:
        return None
    raw = m.group(1)[:4096]
    enc = "utf-8"
    if content_type:
        cm = _CHARSET_RE.search(content_type)
        if cm:
            enc = cm.group(1)
    try:
        text = raw.decode(enc, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return clean_text(html.unescape(text))


def banner_from_response(status: int, headers: dict, body: bytes) -> dict | None:
    if not status:
        return None
    return {"title": extract_title(body, headers.get("content-type")),
            "server": clean_text(headers.get("server"))}


async def fetch_banner(ip: str, port: int, tls: bool, timeout: float = 2.0) -> dict | None:
    """Return {"title", "server"} or None when nothing usable came back."""
    res = await http_get(ip, port, tls=tls, path="/", cap=BANNER_CAP, timeout=timeout)
    if res is None:
        return None
    return banner_from_response(res.status, res.headers, res.body)
