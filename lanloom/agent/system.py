"""Host facts of the agent: name, kernel, addresses, load, processor use, memory, disks, temperature and throttling.
Read from the kernel's files only.

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


_CPU_LAST: dict = {}
HOT_ZONES = ("cpu", "soc", "pkg", "core", "x86", "acpitz", "package")


def cpu_percent(proc: str = "/proc") -> float | None:
    """Processor use since the previous call (all cores, from the first line of /proc/stat); None on the first call."""
    parts = _read(proc + "/stat", 4096).split("\n", 1)[0].split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        vals = [int(x) for x in parts[1:9]]
    except ValueError:
        return None
    idle, total = vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
    last = _CPU_LAST.get(proc)
    _CPU_LAST[proc] = (idle, total)
    if not last or total <= last[1]:
        return None
    return round(max(0.0, min(100.0, 100.0 * (1 - (idle - last[0]) / (total - last[1])))), 1)


def temperature(sys_class: str = "/sys/class/thermal") -> float | None:
    """Highest processor-near thermal zone in degrees Celsius (any zone when none is named like a processor)."""
    hot, other = [], []
    try:
        names = sorted(n for n in os.listdir(sys_class) if n.startswith("thermal_zone"))[:32]
    except OSError:
        return None
    for n in names:
        try:
            v = int(_read("%s/%s/temp" % (sys_class, n), 32).strip()) / 1000.0
        except ValueError:
            continue
        if not 0 < v < 150:
            continue
        kind = _read("%s/%s/type" % (sys_class, n), 64).strip().lower()
        (hot if any(k in kind for k in HOT_ZONES) else other).append(v)
    pick = hot or other
    return round(max(pick), 1) if pick else None


def throttle(sys_root: str = "/sys") -> dict:
    """Is the processor held back? ``{"throttled": bool|None, "freq_pct": share of the highest clock}``. The firmware flag
    exists on some single-board computers only; the clock share works wherever cpufreq is there."""
    out = {"throttled": None, "freq_pct": None}
    raw = _read(sys_root + "/devices/platform/soc/soc:firmware/get_throttled", 32).strip()
    if raw:
        try:
            out["throttled"] = bool(int(raw, 16) & 0x6)      # bit 1: frequency capped, bit 2: throttled right now
        except ValueError:
            pass
    try:
        cur = int(_read(sys_root + "/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", 32).strip())
        top = int(_read(sys_root + "/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", 32).strip())
        if top > 0 and 0 < cur <= top * 1.2:
            out["freq_pct"] = round(min(100.0, 100.0 * cur / top), 1)
    except ValueError:
        pass
    return out


def sample() -> dict:
    out = {"load": load(), "disks": disks(), "cpu": cpu_percent(), "temp": temperature()}
    out.update(meminfo())
    out.update(throttle())
    return out
