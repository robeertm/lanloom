"""Entry point: `python -m updater` (loop) or `python -m updater --once-check` (diagnostics).

Environment:
  LANLOOM_DATA                 shared data volume inside the sidecar (default /data)
  LANLOOM_TARGET               name of the app container (default: find it by label org.lanloom.role=app)
  LANLOOM_RUNTIME_SOCKET       path of the container runtime socket
  LANLOOM_UPDATER_POLL         seconds between request polls (default 2)
  LANLOOM_HEALTH_URL           tests only: fixed health URL of the app
  LANLOOM_UPDATER_ALLOW_LOCAL  tests only: "1" accepts local repository names (no pull)
"""

from __future__ import annotations

import argparse
import json
import signal
import sys

from . import sidecar_version
from .engine import Settings, Updater
from .runtimeapi import RuntimeAPI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m updater", description="Lanloom update sidecar")
    parser.add_argument("--once-check", action="store_true", help="print a diagnostic report and exit")
    parser.add_argument("--version", action="store_true", help="print the sidecar version and exit")
    args = parser.parse_args(argv)

    if args.version:
        print(sidecar_version())
        return 0

    settings = Settings.from_env()
    updater = Updater(RuntimeAPI(settings.socket_path), settings)

    if args.once_check:
        report = updater.check()
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report.get("ok") else 1

    def _stop(signum, frame):  # noqa: ARG001
        updater.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if settings.allow_local:
        print("updater: LANLOOM_UPDATER_ALLOW_LOCAL=1 (test mode: local repositories, no pull)", flush=True)
    print(f"updater: started, version {sidecar_version()}", flush=True)
    updater.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
