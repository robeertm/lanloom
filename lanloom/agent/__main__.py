"""``python -m lanloom.agent run | once | selftest``

run       pair with LANLOOM_AGENT_CODE (first start) and report to LANLOOM_AGENT_SERVER until stopped
once      print one complete report as JSON (what the agent would send; nothing is sent)
selftest  check the secret filter with built-in samples (no network)
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time

from .. import __version__


def _once() -> int:
    from .runner import Collector
    col = Collector()
    col.poll()
    time.sleep(2.0)
    col.poll()
    rep = col.report(full=True)
    from . import scrub
    bad = scrub.check(rep)
    print(json.dumps(rep, indent=1, sort_keys=True))
    return 1 if bad else 0


def selftest() -> int:
    from . import containers, scrub
    row = {"Id": "a" * 64, "Names": ["/web"], "Image": "example/web:1", "State": "running", "Status": "Up 3 hours (healthy)",
           "Command": "serve --password hunter2-secret", "Labels": {"com.docker.compose.project": "site", "api_token": "x"},
           "Mounts": [{"Source": "/srv/secret"}], "Env": ["DB_PASSWORD=s3cr3t-value"], "Created": 1}
    item = containers.parse_list_row(row)
    flat = json.dumps(item)
    ok = item and item["health"] == "healthy" and item.get("project") == "site"
    ok = ok and not any(s in flat for s in ("hunter2", "s3cr3t", "/srv/secret", "api_token", "Command", "Env"))
    ok = ok and scrub.text("db password=abc123") == "db password=[hidden]"
    ok = ok and scrub.check({"a": [{"env": 1}]}) == ["/a[0]/env"]
    print("agent selftest: %s" % ("ok" if ok else "FAILED"))
    return 0 if ok else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "run"
    logging.basicConfig(level=os.environ.get("LANLOOM_AGENT_LOG", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    if cmd == "once":
        return _once()
    if cmd == "selftest":
        return selftest()
    if cmd == "version":
        print(__version__)
        return 0
    if cmd != "run":
        print(__doc__)
        return 64
    from .transport import Link, PairError, STATE_DIR
    from .runner import run
    try:
        link = Link(os.environ.get("LANLOOM_AGENT_SERVER", ""), os.environ.get("LANLOOM_AGENT_STATE", STATE_DIR),
                    __version__, os.environ.get("LANLOOM_AGENT_PIN", ""))
    except PairError:
        logging.getLogger("lanloom.agent").error("agent: LANLOOM_AGENT_SERVER must be the http(s) address of Lanloom")
        return 2
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    return run(link, os.environ.get("LANLOOM_AGENT_CODE", ""), stop=lambda: bool(stopping))


if __name__ == "__main__":
    sys.exit(main())
