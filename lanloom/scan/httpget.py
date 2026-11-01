"""Tiny read-only HTTP(S) GET client used for web banners and UPnP descriptions.

* GET only, HTTP/1.0 with `Connection: close`, no cookies, no authentication.
* Hard size cap and overall timeout.
* Redirects are followed only to the very same IP address (at most 2 hops).
* TLS is used without certificate verification (devices on a LAN carry
  self-signed certificates); nothing secret is ever sent.
"""
from __future__ import annotations

import asyncio
import ipaddress
import ssl
import urllib.parse

USER_AGENT = "lanloom-discovery/1"
MAX_HEADER_BYTES = 16 * 1024
MAX_REDIRECTS = 2


class HTTPResult:
    __slots__ = ("status", "headers", "body", "url")

    def __init__(self, status: int, headers: dict, body: bytes, url: str):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url

    def __repr__(self) -> str:
        return f"HTTPResult(status={self.status}, bytes={len(self.body)})"


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def parse_head(raw: bytes) -> tuple[int, dict]:
    """Parse status line + headers. Header names are lower-cased; first value wins."""
    text = raw.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    first = lines[0].split(None, 2) if lines else []
    status = 0
    if len(first) >= 2 and first[0].upper().startswith("HTTP/"):
        try:
            status = int(first[1])
        except ValueError:
            status = 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower()
        if k and k not in headers and len(headers) < 100:
            headers[k] = v.strip()[:1024]
    return status, headers


def same_host_url(base_ip: str, url: str) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        return ipaddress.ip_address(u.hostname) == ipaddress.ip_address(base_ip)
    except ValueError:
        return False


async def _fetch_once(ip: str, port: int, tls: bool, path: str, cap: int, timeout: float) -> tuple[int, dict, bytes]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(ip, port, ssl=_tls_context() if tls else None,
                                server_hostname="" if tls else None),
        timeout)
    try:
        host = f"[{ip}]" if ":" in ip else ip
        req = (f"GET {path} HTTP/1.0\r\nHost: {host}:{port}\r\nUser-Agent: {USER_AGENT}\r\n"
               f"Accept: text/html,text/xml,application/xml;q=0.9,*/*;q=0.5\r\nConnection: close\r\n\r\n")
        writer.write(req.encode("ascii", errors="replace"))
        await writer.drain()
        buf = bytearray()
        head_end = -1
        while len(buf) < cap + MAX_HEADER_BYTES:
            chunk = await reader.read(8192)
            if not chunk:
                break
            buf += chunk
            if head_end < 0:
                head_end = buf.find(b"\r\n\r\n")
                if head_end < 0 and len(buf) > MAX_HEADER_BYTES:
                    break
            if head_end >= 0 and len(buf) - head_end - 4 >= cap:
                break
        if head_end < 0:
            sep = buf.find(b"\n\n")
            if sep < 0:
                return parse_head(bytes(buf[:MAX_HEADER_BYTES]))[0], {}, b""
            status, headers = parse_head(bytes(buf[:sep]))
            return status, headers, bytes(buf[sep + 2:sep + 2 + cap])
        status, headers = parse_head(bytes(buf[:head_end]))
        return status, headers, bytes(buf[head_end + 4:head_end + 4 + cap])
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 0.5)
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            pass


async def http_get(ip: str, port: int, tls: bool = False, path: str = "/", cap: int = 32 * 1024,
                   timeout: float = 2.0, follow_redirects: bool = True) -> HTTPResult | None:
    """GET `path` from ip:port. Returns None on any network/protocol error."""
    if not path.startswith("/"):
        path = "/" + path
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    scheme = "https" if tls else "http"
    for _hop in range(MAX_REDIRECTS + 1):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            status, headers, body = await asyncio.wait_for(
                _fetch_once(ip, port, tls, path, cap, remaining), remaining)
        except (OSError, asyncio.TimeoutError, ssl.SSLError, EOFError, ValueError, UnicodeError):
            return None
        url = f"{scheme}://{ip}:{port}{path}"
        if follow_redirects and status in (301, 302, 303, 307, 308) and headers.get("location"):
            target = urllib.parse.urljoin(url, headers["location"])
            if not same_host_url(ip, target):
                return HTTPResult(status, headers, body, url)
            u = urllib.parse.urlsplit(target)
            scheme = u.scheme
            tls = scheme == "https"
            try:
                port = u.port or (443 if tls else 80)
            except ValueError:
                return HTTPResult(status, headers, body, url)
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            continue
        return HTTPResult(status, headers, body, url)
    return None
