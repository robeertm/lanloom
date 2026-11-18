#!/usr/bin/env python3
"""Start script of the Lanloom add-on.

Runs as root only long enough to read the add-on options, create Lanloom's folder in the add-on volume and give it to
the unprivileged user of the image (uid/gid 10001). Then it drops every privilege and replaces itself with Lanloom.

* Data: ``/data/lanloom`` (inside the add-on volume, so backups of the add-on system include it).
* Updates: come from the add-on system; Lanloom's own updater and release check are off in add-on mode.
* Sign-in: through the ingress proxy of the add-on system (fixed address of the add-on network, see
  lanloom/config.py); the role of people who open the panel comes from the option ``ingress_role``.
* Direct access: off by default - only the ingress proxy is answered. ``direct_access: true`` also answers the
  network on port 7380 (with Lanloom's own accounts).
"""
import json
import os
import sys

OPTIONS = "/data/options.json"
DATA = "/data/lanloom"
UID = GID = 10001
ROLES = ("viewer", "operator", "admin")


def options(path: str = OPTIONS) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    role = str(raw.get("ingress_role") or "admin").strip().lower()
    return {"ingress_role": role if role in ROLES else "admin", "direct_access": raw.get("direct_access") is True}


def environment(opts: dict, base: dict) -> dict:
    env = dict(base)
    env.update({
        "LANLOOM_ADDON": "1",
        "LANLOOM_DATA": DATA,
        "LANLOOM_PORT": "7380",
        "LANLOOM_HOST": "0.0.0.0",
        "LANLOOM_AUTH_DEFAULT_ROLE": opts["ingress_role"],
        "LANLOOM_ADDON_LAN": "1" if opts["direct_access"] else "0",
        "LANLOOM_UPDATE_CHECK": "0",
        "HOME": "/tmp",
    })
    return env


def prepare(path: str = DATA, uid: int = UID, gid: int = GID) -> None:
    os.makedirs(path, mode=0o750, exist_ok=True)
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, n) for n in dirs + files]:
            try:
                st = os.lstat(name)
                if (st.st_uid, st.st_gid) != (uid, gid):
                    os.lchown(name, uid, gid)
            except OSError:
                pass
    os.chmod(path, 0o750)


def drop(uid: int = UID, gid: int = GID) -> None:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.getuid() != uid or os.geteuid() != uid:
        raise SystemExit("could not drop privileges")


def main() -> None:
    opts = options()
    env = environment(opts, os.environ)
    if os.geteuid() == 0:
        prepare()
        drop()
    os.chdir("/app")
    os.execvpe(sys.executable, [sys.executable, "-m", "lanloom"], env)


if __name__ == "__main__":
    main()
