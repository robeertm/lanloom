"""Segmentation plan: which network each device should end up in, and how far the move has come.

A plan is Lanloom's own note, never a controller setting: it can be kept with a read-only connection too (and is the
first step before anything is changed). Per device it names an existing network (``{"net": id}``) or a network that does
not exist yet (``{"label": "IoT"}``); a label counts as reached once a network of that name exists and the device is in
it. The firewall tab shows the target as a mark on the device ("→ IoT"), the progress ("5 of 12 moved") and offers to
draft the moves that are possible now (at most one plan's worth at a time). Stored in the settings table, keyed by the
device's opaque id (no addresses).
"""
import time

from . import model

KEY = "fw_segplan"
PROGRESS_KEY = "fw_segplan_progress"      # last progress the editor saw (for the nightly report, which has no state)
MAX_ENTRIES = 2000
LABEL_MAX = 32


def load(store) -> dict:
    d = store.get_setting(KEY, None)
    if not isinstance(d, dict) or not isinstance(d.get("targets"), dict):
        return {"targets": {}, "t": 0}
    return d


def clean_target(target):
    """{"net": id} | {"label": text} | None (remove). Raises ValueError on anything else."""
    if target is None:
        return None
    if not isinstance(target, dict) or len(target) != 1:
        raise ValueError("segplan.err.target")
    if "net" in target:
        v = target["net"]
        if not isinstance(v, str) or not 0 < len(v) <= 96:
            raise ValueError("segplan.err.target")
        return {"net": v}
    if "label" in target:
        v = target["label"]
        if not isinstance(v, str):
            raise ValueError("segplan.err.target")
        v = " ".join(v.split())[:LABEL_MAX]
        if not v or any(ord(c) < 32 for c in v):
            raise ValueError("segplan.err.target")
        return {"label": v}
    raise ValueError("segplan.err.target")


def set_targets(store, items: dict, now: float = None) -> dict:
    """``items``: {device id: target or None}. Returns the stored plan."""
    plan = load(store)
    targets = dict(plan["targets"])
    for dev_id, target in (items or {}).items():
        if not isinstance(dev_id, str) or not 0 < len(dev_id) <= 96:
            raise ValueError("segplan.err.device")
        t = clean_target(target)
        if t is None:
            targets.pop(dev_id, None)
        else:
            targets[dev_id] = dict(t, t=int(now or time.time()))
    if len(targets) > MAX_ENTRIES:
        raise ValueError("segplan.err.too_many")
    plan = {"targets": targets, "t": int(now or time.time())}
    store.set_setting(KEY, plan)
    return plan


def _resolve(state: dict, target: dict):
    """The network a target points to in this state, or None (a label without such a network yet)."""
    nets = state.get("networks") or []
    if "net" in target:
        return next((n for n in nets if n.get("id") == target["net"]), None)
    want = target.get("label", "").strip().lower()
    return next((n for n in nets if str(n.get("name") or "").strip().lower() == want), None)


def status(state: dict, plan: dict) -> dict:
    """Progress of the plan against a state: {"total", "moved", "pending", "elsewhere", "targets": [{"key", "label",
    "net", "total", "moved"}], "devices": {id: {"target", "net", "label", "moved", "possible"}}, "moves": {id: net}}.

    ``elsewhere`` are planned devices this state does not know (another controller, a device that left).
    ``moves`` holds what can be drafted now: devices not moved yet whose target network exists and takes them."""
    devs = {d["id"]: d for d in state.get("devices") or [] if isinstance(d, dict) and d.get("id")}
    out = {"total": 0, "moved": 0, "pending": 0, "elsewhere": 0, "targets": [], "devices": {}, "moves": {}}
    groups = {}
    for dev_id, target in sorted((plan or {}).get("targets", {}).items()):
        if not isinstance(target, dict):
            continue
        dev = devs.get(dev_id)
        if dev is None:
            out["elsewhere"] += 1
            continue
        net = _resolve(state, target)
        label = net["name"] if net else target.get("label") or "?"
        key = ("n:" + net["id"]) if net else ("l:" + label.lower())
        moved = bool(net) and dev.get("network") == net["id"]
        possible = False
        if net and not moved:
            try:
                possible = model.drop_targets(state, dev).get(net["id"]) is None
            except Exception:
                possible = False
            if possible:
                out["moves"][dev_id] = net["id"]
        out["devices"][dev_id] = {"label": label, "net": net["id"] if net else None, "moved": moved, "possible": possible,
                                  "planned": "label" in target and net is None}
        g = groups.setdefault(key, {"key": key, "label": label, "net": net["id"] if net else None, "total": 0, "moved": 0})
        g["total"] += 1
        out["total"] += 1
        if moved:
            g["moved"] += 1
            out["moved"] += 1
        else:
            out["pending"] += 1
    out["targets"] = sorted(groups.values(), key=lambda g: (g["moved"] == g["total"], g["label"].lower()))
    first = dict(list(out["moves"].items())[:model.MAX_OBJECTS])
    out["moves"] = first
    return out


def remember(store, st: dict, now: float = None) -> None:
    """Keep the progress the editor just computed for the nightly report (written only when it changed)."""
    groups = [{"label": g["label"], "total": g["total"], "moved": g["moved"]} for g in st.get("targets") or []][:24]
    cur = {"total": st.get("total", 0), "moved": st.get("moved", 0), "groups": groups}
    old = store.get_setting(PROGRESS_KEY, None)
    if isinstance(old, dict) and {k: old.get(k) for k in cur} == cur:
        return
    store.set_setting(PROGRESS_KEY, dict(cur, t=int(now or time.time())))


def progress(store):
    """The progress last seen by the editor, or None when there is no plan."""
    d = store.get_setting(PROGRESS_KEY, None)
    if not isinstance(d, dict) or not d.get("total"):
        return None
    return d
