"""Shared helper: one unicast UDP question per host, answers matched by sender and token."""
from __future__ import annotations

import asyncio
import ipaddress
import socket


def as_bucket(limiter_or_bucket):
    """Accept a `Limiter` (uses its token bucket) or a `TokenBucket`."""
    return getattr(limiter_or_bucket, "bucket", limiter_or_bucket)


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, port, pending, results, parse):
        self._port, self._pending, self._results, self._parse = port, pending, results, parse

    def datagram_received(self, data, addr):
        if not isinstance(addr, tuple) or len(addr) < 2 or addr[1] != self._port:
            return
        ip = addr[0]
        token = self._pending.get(ip)
        if token is None or ip in self._results:
            return
        try:
            res = self._parse(data, token)
        except Exception:      # hostile payloads must never escape the datagram callback
            res = None
        if res is not None:
            self._results[ip] = res

    def error_received(self, exc):
        pass


async def ask(ips, port: int, make, parse, bucket, timeout: float, on_sent=None) -> dict:
    """Send exactly one datagram per IPv4 address in `ips`; return {ip: parsed answer}.

    `make(ip)` -> (token, payload); `parse(data, token)` -> result or None. Only answers coming from the
    asked address and `port` count. Waits up to `timeout` seconds after the last datagram.
    """
    targets = []
    for ip in ips:
        try:
            if ipaddress.ip_address(ip).version == 4 and ip not in targets:
                targets.append(ip)
        except ValueError:
            continue
    if not targets or timeout <= 0:
        return {}
    bucket = as_bucket(bucket)
    loop = asyncio.get_running_loop()
    results: dict = {}
    pending: dict = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    transport = None
    try:
        sock.setblocking(False)
        sock.bind(("0.0.0.0", 0))
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Proto(port, pending, results, parse), sock=sock)
        sock = None
        for ip in targets:
            await bucket.acquire()
            token, payload = make(ip)
            pending[ip] = token
            try:
                transport.sendto(payload, (ip, port))
            except OSError:
                pending.pop(ip, None)
            if on_sent:
                on_sent()
        deadline = loop.time() + timeout
        while loop.time() < deadline and len(results) < len(pending):
            await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))
    except OSError:
        pass
    finally:
        if transport is not None:
            transport.close()
        if sock is not None:
            sock.close()
    return results


def clean_text(value, limit: int) -> str | None:
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in str(value)).split())
    return text[:limit] or None
