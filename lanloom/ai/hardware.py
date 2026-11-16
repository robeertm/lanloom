"""Model size advice from the target computer's memory, cores and graphics.

Measured on small computers: without a graphics unit the number of cores is the limit, not the memory; reading the
prompt is the slow part. Advice is honest: below 4 GB nothing local is offered, below 8 GB a small model with a
warning, and without a suitable computer the public services are pointed out.
"""
import os
import platform

# (minimum RAM in GB, model id, approximate download in GB, size class)
TIERS = (
    (24.0, "qwen2.5:14b", 9.0, "large"),
    (8.0, "llama3.1:8b", 4.9, "medium"),
    (4.0, "llama3.2:3b", 2.0, "small"),
)
DEMO_TIERS = (
    (24.0, "sample-large", 9.0, "large"),
    (8.0, "sample-medium", 4.9, "medium"),
    (4.0, "sample-small", 2.0, "small"),
)


def advise(ram_gb, cores, gpu: str = "", arch: str = "", demo: bool = False) -> dict:
    """``{"model", "size", "download_gb", "speed": "good"|"ok"|"slow"|"unsuitable", "notes": [i18n keys]}``."""
    try:
        ram = float(ram_gb or 0)
    except (TypeError, ValueError):
        ram = 0.0
    try:
        n = int(cores or 0)
    except (TypeError, ValueError):
        n = 0
    gpu = str(gpu or "").lower()
    arch = str(arch or "").lower()
    has_gpu = bool(gpu) and gpu not in ("none", "unknown", "no")
    tiers = DEMO_TIERS if demo else TIERS
    notes = []
    if ram and ram < 4.0:
        return {"model": "", "size": "", "download_gb": 0, "speed": "unsuitable", "ram_gb": round(ram, 1), "cores": n,
                "gpu": gpu, "notes": ["ai.hw.too_small", "ai.hw.use_public"]}
    pick = tiers[-1]
    for tier in tiers:
        if ram >= tier[0] and (tier[3] != "large" or has_gpu):
            pick = tier
            break
    if not ram:
        pick = tiers[1]
        notes.append("ai.hw.unknown_ram")
    if has_gpu:
        speed = "good"
    elif n >= 8:
        speed = "ok"
    else:
        speed = "slow"
    if pick[3] == "small":
        notes.append("ai.hw.small_model")
    if speed == "slow":
        notes.append("ai.hw.slow")
    if arch.startswith(("arm", "aarch")) and not has_gpu and ram < 8:
        notes.append("ai.hw.small_board")
    if speed == "slow" or pick[3] == "small":
        notes.append("ai.hw.consider_other")
    return {"model": pick[1], "size": pick[3], "download_gb": pick[2], "speed": speed, "ram_gb": round(ram, 1),
            "cores": n, "gpu": gpu, "notes": notes}


def this_host() -> dict:
    """Memory and cores of the computer Lanloom runs on (host networking shares the host's memory view)."""
    ram = 0.0
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    ram = int(line.split()[1]) / 1024 / 1024
                    break
    except (OSError, ValueError, IndexError):
        pass
    limit = _cgroup_limit_gb()
    if limit and (not ram or limit < ram):
        ram = limit
    return {"ram_gb": round(ram, 1), "cores": os.cpu_count() or 0, "gpu": "", "arch": platform.machine()}


def _cgroup_limit_gb() -> float:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = open(path, encoding="ascii").read().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 50:
            return int(raw) / 1024 ** 3
    return 0.0
