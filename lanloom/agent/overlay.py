"""Peers of an overlay VPN on the agent's host, read from the VPN daemon's local status interface (read only).

Only when that interface is there (its socket mounted into the agent, or set with ``LANLOOM_AGENT_OVERLAY``). The
agent asks for the status once a minute with GET and keeps per peer: name, overlay addresses, online, last seen and
the operating system label. Keys, user names, log-in names and routes are never copied.
"""
from __future__ import annotations

import http.client
import json
import os
import socket

from . import scrub

SOCKETS = ("/var/run/tailscale/tailscaled.sock", "/run/tailscale/tailscaled.sock")  # identifier: daemon socket path
HOST = "local-tailscaled.sock"  # identifier: host name the daemon's local interface expects
STATUS = "/localapi/v0/status"  # identifier: status path of the local interface
MAX_PEERS = 300


class _Unix(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__(HOST, timeout=4.0)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def _ts(v):
    from .containers import _ts as ts
    got = ts(v)
    return round(got) if got else None


def parse(status: dict) -> dict:
    peers = []
    for p in list((status.get("Peer") or {}).values())[:MAX_PEERS]:
        if not isinstance(p, dict):
            continue
        name = p.get("HostName") or (p.get("DNSName") or "").split(".")[0]
        ips = [scrub.text(ip, 45) for ip in (p.get("TailscaleIPs") or [])[:4]]  # identifier: field name of the status answer
        peers.append({"name": scrub.text(name, 64), "ips": ips, "online": bool(p.get("Online")),
                      "last_seen": _ts(p.get("LastSeen")), "os": scrub.text(p.get("OS"), 16)})
    self_ = status.get("Self") or {}
    return {"available": True, "self": {"name": scrub.text(self_.get("HostName"), 64),
                                        "ips": [scrub.text(ip, 45) for ip in (self_.get("TailscaleIPs") or [])[:4]]},  # identifier
            "peers": peers}


def collect() -> dict:
    path = os.environ.get("LANLOOM_AGENT_OVERLAY", "")
    if not path:
        path = next((p for p in SOCKETS if os.path.exists(p)), "")
    if not path or not os.path.exists(path):
        return {"available": False}
    conn = _Unix(path)
    try:
        conn.request("GET", STATUS, headers={"Host": HOST})
        resp = conn.getresponse()
        raw = resp.read(8 << 20)
        if resp.status != 200:
            return {"available": False, "reason": "status"}
        return parse(json.loads(raw.decode("utf-8", "replace")))
    except (OSError, http.client.HTTPException, ValueError):
        return {"available": False, "reason": "unreachable"}
    finally:
        conn.close()
