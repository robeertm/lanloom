"""Host facts of the agent: name, kernel, addresses, load, memory and disks. Read from the kernel's files only.

Nothing here runs a program. Disks are the file systems under the paths in ``LANLOOM_AGENT_DISKS`` (default: the
host's root mounted read-only at ``/host``, else the agent's own root).
"""
from __future__ import annotations

import os
import platform
import socket

from . import scrub

DEFAULT_DISKS = ("/host", "/")
SKIP_IFACES = ("lo", "veth", "docker", "br-", "virbr", "cni", "flannel", "cali", "vxlan", "tun", "kube")


def _read(path: str, limit: int = 1 << 16) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def hostname(proc: str = "/proc") -> str:
    name = os.environ.get("LANLOOM_AGENT_NAME") or _read(proc + "/sys/kernel/hostname").strip() or socket.gethostname()
    return scrub.text(name, 64) or "host"


def os_name(root: str = "") -> str:
    for path in (root + "/host/etc/os-release", root + "/etc/os-release"):
        for line in _read(path).splitlines():
            if line.startswith("PRETTY_NAME="):
                return scrub.text(line.split("=", 1)[1].strip().strip('"'), 64)
    return ""


def load(proc: str = "/proc") -> list | None:
    parts = _read(proc + "/loadavg").split()
    try:
        return [round(float(x), 2) for x in parts[:3]]
    except (ValueError, IndexError):
        return None


def meminfo(proc: str = "/proc") -> dict:
    out = {}
    for line in _read(proc + "/meminfo").splitlines():
        k, _, rest = line.partition(":")
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            try:
                out[k] = int(rest.split()[0]) * 1024
            except (ValueError, IndexError):
                pass
    return {"mem_total": out.get("MemTotal"), "mem_avail": out.get("MemAvailable"),
            "swap_total": out.get("SwapTotal"), "swap_free": out.get("SwapFree")}


def uptime(proc: str = "/proc") -> float | None:
    try:
        return round(float(_read(proc + "/uptime").split()[0]))
    except (ValueError, IndexError):
        return None


def disks(paths=None) -> list:
    raw = os.environ.get("LANLOOM_AGENT_DISKS", "")
    paths = paths or ([p for p in raw.split(",") if p.strip()] if raw else None)
    if not paths:
        paths = ["/host"] if os.path.isdir("/host") else ["/"]
    out, seen = [], set()
    for p in paths[:16]:
        p = p.strip()
        try:
            st = os.statvfs(p)
        except OSError:
            continue
        key = (st.f_blocks, st.f_bfree, st.f_frsize)
        if key in seen or not st.f_blocks:
            continue
        seen.add(key)
        label = "/" if p in ("/host", "/") else (p[5:] if p.startswith("/host/") else p)
        out.append({"mount": scrub.text(label, 64), "total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize})
    return out


def _iface_ip(name: str) -> str:
    """IPv4 address of one interface (SIOCGIFADDR), "" without one."""
    try:
        import fcntl
        import struct
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            raw = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))
        return socket.inet_ntoa(raw[20:24])
    except (OSError, ImportError, ValueError):
        return ""


def _skip(name: str) -> bool:
    return any(name == s or name.startswith(s) for s in SKIP_IFACES)


def addresses(sys_net: str = "/sys/class/net", proc: str = "/proc") -> tuple:
    """(IPv4 addresses, hardware addresses) of the host's real interfaces (no loopback, bridges or container pairs)."""
    macs = []
    try:
        names = sorted(os.listdir(sys_net))
    except OSError:
        names = []
    for name in names:
        if _skip(name):
            continue
        mac = _read("%s/%s/address" % (sys_net, name)).strip().lower()
        if mac and mac != "00:00:00:00:00:00" and len(mac) == 17:
            macs.append(mac)
    ips = []
    for name in names:
        if _skip(name):
            continue
        ip = _iface_ip(name)
        if ip and not ip.startswith("127.") and ip not in ips:
            ips.append(ip)
    return ips[:16], macs[:16]


def facts() -> dict:
    ips, macs = addresses()
    return {"name": hostname(), "kernel": scrub.text(platform.release(), 64), "os": os_name(),
            "arch": scrub.text(platform.machine(), 16), "cpus": os.cpu_count() or 1, "uptime": uptime(),
            "ips": ips, "macs": macs}


def sample() -> dict:
    out = {"load": load(), "disks": disks()}
    out.update(meminfo())
    return out
