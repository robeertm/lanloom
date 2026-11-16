"""Certificate expiry of the TLS endpoints Lanloom itself connects to (hub, controller, broker, adapters).

One handshake per endpoint, once per report, without verification (the point is to read the expiry date, also of
a self-signed certificate the user accepted). Only addresses the user entered are contacted.
"""
import socket
import ssl
import time
from datetime import timezone
from urllib.parse import urlparse

MAX_ENDPOINTS = 12
TIMEOUT = 4.0


def endpoints(integrations: list) -> list:
    out, seen = [], set()
    for integ in integrations:
        if not integ.get("enabled"):
            continue
        cfg = integ.get("config") or {}
        host, port = None, None
        url = str(cfg.get("url") or "")
        if url.startswith("https://"):
            p = urlparse(url)
            host, port = p.hostname, p.port or 443
        elif integ.get("kind") == "mqtt" and cfg.get("tls") and cfg.get("host"):
            host, port = str(cfg["host"]), int(cfg.get("port") or 8883)
        if host and (host, port) not in seen:
            seen.add((host, port))
            out.append({"id": "int:%d" % integ["id"], "name": integ.get("name") or host, "host": host, "port": port})
    return out[:MAX_ENDPOINTS]


def days_left(host: str, port: int, now: float = None) -> int:
    from cryptography import x509
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    end = cert.not_valid_after_utc.replace(tzinfo=timezone.utc).timestamp()
    return int((end - (time.time() if now is None else now)) // 86400)


def check_all(eps: list) -> list:
    out = []
    for ep in eps:
        try:
            days = days_left(ep["host"], ep["port"])
        except Exception:
            days = None
        out.append({"id": ep["id"], "name": ep["name"], "days": days})
    return out
