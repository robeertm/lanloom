"""TCP port lists and the connect probe.

A probe is a plain TCP connect (no payload) that is closed right away.
ECONNREFUSED proves the host is alive even though the port is closed.
"""
from __future__ import annotations

import asyncio
import errno
import socket
import time

# Gentle list: common home / small-office services, neutral service names.
GENTLE_PORTS: dict[int, str] = {
    21: "ftp",
    22: "ssh",
    23: "telnet",
    53: "dns",
    80: "http",
    81: "web-alt",
    443: "https",
    445: "smb",
    554: "rtsp",
    631: "ipp",
    1883: "mqtt",
    3000: "web-alt",
    3389: "remote-desktop",
    5000: "web-alt",
    5001: "web-alt-tls",
    5900: "vnc",
    8008: "web-alt",
    8080: "http-alt",
    8081: "web-alt",
    8123: "web-alt",
    8443: "https-alt",
    8883: "mqtts",
    9000: "web-alt",
    9100: "printer-raw",
    32400: "media-server",
    49152: "upnp",
}

# Additional ports for the deep scan (only with explicit consent).
_DEEP_EXTRA: dict[int, str] = {
    7: "echo", 20: "ftp-data", 25: "smtp", 37: "time", 69: "tftp", 79: "finger", 88: "kerberos",
    110: "pop3", 111: "rpcbind", 113: "ident", 119: "nntp", 123: "ntp", 135: "rpc", 137: "netbios-ns",
    139: "netbios", 143: "imap", 161: "snmp", 179: "bgp", 199: "smux", 389: "ldap", 427: "slp",
    464: "kpasswd", 465: "smtps", 497: "backup", 500: "ike", 513: "rlogin", 514: "syslog", 515: "lpd",
    543: "klogin", 544: "kshell", 548: "file-share", 587: "submission", 593: "rpc-http", 623: "ipmi",
    636: "ldaps", 646: "ldp", 873: "rsync", 902: "vm-console", 990: "ftps", 993: "imaps", 995: "pop3s",
    1025: "rpc-alt", 1026: "rpc-alt", 1027: "rpc-alt", 1080: "socks", 1110: "web-alt", 1194: "vpn",
    1352: "groupware", 1400: "media-ctl", 1433: "db", 1434: "db", 1521: "db", 1701: "l2tp", 1723: "pptp",
    1720: "h323", 1755: "streaming", 1812: "radius", 1813: "radius-acct", 1900: "upnp",
    2000: "voip-alt", 2049: "nfs", 2082: "web-alt", 2083: "web-alt-tls", 2086: "web-alt", 2087: "web-alt-tls",
    2121: "ftp-alt", 2181: "coordination", 2222: "ssh-alt", 2375: "container-api", 2376: "container-api-tls",
    2379: "kv-store", 2380: "kv-store", 2869: "upnp", 3001: "web-alt", 3128: "proxy", 3260: "iscsi",
    3268: "ldap-gc", 3306: "db", 3478: "stun", 3483: "media-ctl", 3689: "media-share", 3690: "version-control",
    4000: "web-alt", 4040: "web-alt", 4443: "https-alt", 4444: "web-alt", 4567: "web-alt",
    4711: "web-alt", 4848: "web-admin", 4899: "remote-admin", 5002: "web-alt", 5003: "web-alt",
    5004: "rtp", 5005: "rtp", 5050: "web-alt", 5060: "sip", 5061: "sips", 5080: "web-alt",
    5222: "xmpp", 5223: "push", 5269: "xmpp-server", 5280: "xmpp-web", 5353: "mdns",
    5357: "wsd", 5432: "db", 5555: "debug-bridge", 5601: "web-alt", 5672: "amqp", 5683: "coap",
    5800: "vnc-web", 5901: "vnc", 5902: "vnc", 5938: "remote-desktop", 5984: "db",
    6000: "x11", 6001: "x11", 6379: "kv-store", 6443: "orchestrator-api", 6466: "media-ctl",
    6467: "media-ctl", 6543: "web-alt", 6600: "media-ctl", 6667: "irc", 6668: "irc",
    6789: "web-alt", 7000: "media-ctl", 7001: "web-alt", 7070: "streaming", 7100: "media-ctl",
    7443: "https-alt", 7547: "cwmp", 7681: "web-terminal", 7878: "web-alt", 8000: "http-alt",
    8001: "web-alt", 8002: "web-alt", 8009: "media-ctl", 8010: "web-alt", 8042: "web-alt",
    8060: "media-ctl", 8069: "web-alt", 8083: "web-alt", 8084: "web-alt", 8085: "web-alt",
    8086: "time-series-db", 8088: "web-alt", 8089: "web-alt", 8090: "web-alt", 8091: "web-alt",
    8096: "media-server", 8112: "web-alt", 8181: "web-alt", 8200: "media-server", 8222: "web-alt",
    8282: "web-alt", 8291: "router-admin", 8333: "p2p", 8384: "sync", 8400: "web-alt",
    8444: "https-alt", 8500: "web-alt", 8554: "rtsp-alt", 8686: "web-alt", 8800: "web-alt",
    8880: "web-alt", 8888: "web-alt", 8889: "web-alt", 8899: "web-alt", 8920: "media-server-tls",
    8989: "web-alt", 9001: "web-alt", 9002: "web-alt", 9090: "web-alt", 9091: "web-alt",
    9200: "search", 9443: "https-alt", 9999: "web-alt", 10000: "web-admin", 10001: "device-discovery",
    10243: "media-share", 11211: "cache", 12345: "web-alt", 18080: "web-alt", 20000: "web-alt",
    27017: "db", 37777: "camera-ctl", 49153: "upnp", 49154: "upnp", 50000: "web-alt",
    51413: "p2p", 55443: "https-alt", 62078: "device-sync",
}

DEEP_PORTS: dict[int, str] = dict(sorted({**_DEEP_EXTRA, **GENTLE_PORTS}.items()))

# Ports where an HTTP(S) banner (GET /) is attempted; value = use TLS.
WEB_PORTS: dict[int, bool] = {
    80: False, 81: False, 443: True, 3000: False, 3001: False, 4000: False, 4443: True, 5000: False,
    5001: True, 7443: True, 8000: False, 8008: False, 8080: False, 8081: False, 8088: False,
    8123: False, 8443: True, 8888: False, 9000: False, 9443: True, 32400: False, 49152: False,
    10000: False,
}

# Used to detect live hosts when the kernel neighbour table cannot help (routed ranges).
SWEEP_PORTS: tuple[int, ...] = (80, 443, 22)


def service_name(port: int) -> str:
    return DEEP_PORTS.get(port) or f"tcp-{port}"


OPEN = "open"
REFUSED = "refused"
TIMEOUT = "timeout"
ERROR = "error"


async def tcp_probe(ip: str, port: int, timeout: float) -> tuple[str, float | None]:
    """Connect and close immediately. Returns (state, rtt_ms). rtt is set for open/refused."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(loop.sock_connect(sock, (ip, port)), timeout)
        return OPEN, (time.perf_counter() - t0) * 1000.0
    except ConnectionRefusedError:
        return REFUSED, (time.perf_counter() - t0) * 1000.0
    except asyncio.TimeoutError:
        return TIMEOUT, None
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            return REFUSED, (time.perf_counter() - t0) * 1000.0
        return ERROR, None
    finally:
        sock.close()
