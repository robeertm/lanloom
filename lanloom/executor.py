"""Optional executor service for the firewall editor: ``python -m lanloom.executor <command>``.

Runs as its own container (compose profile ``executor``), next to the web app, never inside it. It holds the only
controller credentials, publishes the controller state to the app, pulls approved plans, verifies the passkey
signature itself and runs the pipeline in ``firewall/runner.py``.

Commands:
  run                 the service loop
  trust-key           read a signing key (JSON exported in the app) from stdin and pin it; prints its fingerprint
  keys                list pinned keys
  status              show target, arming and the last runs

Environment:
  LANLOOM_EXECUTOR_APP          base URL of the web app (default http://127.0.0.1:7380)
  LANLOOM_EXECUTOR_TOKEN[_FILE] shared token, at least 24 characters, same value as in the app
  LANLOOM_EXECUTOR_STATE        state directory (default /state)
  LANLOOM_EXECUTOR_TARGET       simulation (default) | controller
  LANLOOM_EXECUTOR_ADAPTER      package.module:ClassName of a controller adapter (target controller only)
  LANLOOM_EXECUTOR_ADAPTER_OPTIONS  path of a JSON file with the adapter's options (credentials stay there)
  LANLOOM_EXECUTOR_ARM          must be exactly I-UNDERSTAND, and <state>/armed must exist, before real writes
  LANLOOM_EXECUTOR_ORIGINS      comma-separated origins the app is opened from, e.g. https://lanloom.example
  LANLOOM_EXECUTOR_WATCH_S      watch time after a change (default 3600)
  LANLOOM_FW_QUIET_HOURS        e.g. 21-7: changes at night need an explicit confirmation
"""
import json
import logging
import os
import sys
import time
import urllib.request
from pathlib import Path

from . import __version__
from .firewall.adapter import SimulatedController, load_adapter
from .firewall.runner import Runner, read_private, write_private

log = logging.getLogger("lanloom.executor")
ARM_PHRASE = "I-UNDERSTAND"
POLL_S = 10


class Settings:
    def __init__(self, env=None):
        env = env if env is not None else os.environ
        self.app = env.get("LANLOOM_EXECUTOR_APP", "http://127.0.0.1:7380").rstrip("/")
        token_file = env.get("LANLOOM_EXECUTOR_TOKEN_FILE", "").strip()
        self.token = env.get("LANLOOM_EXECUTOR_TOKEN", "").strip()
        if token_file:
            try:
                self.token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError:
                self.token = ""
        self.state = Path(env.get("LANLOOM_EXECUTOR_STATE", "/state"))
        self.target = env.get("LANLOOM_EXECUTOR_TARGET", "simulation").strip().lower()
        self.adapter = env.get("LANLOOM_EXECUTOR_ADAPTER", "").strip()
        self.adapter_options = env.get("LANLOOM_EXECUTOR_ADAPTER_OPTIONS", "").strip()
        self.arm = env.get("LANLOOM_EXECUTOR_ARM", "").strip()
        self.origins = tuple(o.strip().rstrip("/") for o in env.get("LANLOOM_EXECUTOR_ORIGINS", "").split(",") if o.strip())
        try:
            self.watch_s = int(env.get("LANLOOM_EXECUTOR_WATCH_S", "3600"))
        except ValueError:
            self.watch_s = 3600
        self.quiet = env.get("LANLOOM_FW_QUIET_HOURS", "").strip()

    def armed(self) -> bool:
        """Real writes need the phrase in the environment AND a file the operator created by hand."""
        return self.target == "controller" and self.arm == ARM_PHRASE and (self.state / "armed").is_file()


def keys_path(s: Settings) -> Path:
    return s.state / "signing-keys.json"


def build_adapter(s: Settings):
    if s.target == "simulation":
        saved = read_private(s.state / "simulation.json", None)
        return SimulatedController(state=saved, persist=lambda st: write_private(s.state / "simulation.json", st), settle_s=5)
    if s.target != "controller":
        raise SystemExit("LANLOOM_EXECUTOR_TARGET must be simulation or controller")
    if not s.armed():
        raise SystemExit("Real writes are not armed. Set LANLOOM_EXECUTOR_ARM=%s and create %s by hand "
                         "after reading docs/FIREWALL-EDITOR.md." % (ARM_PHRASE, s.state / "armed"))
    options = {}
    if s.adapter_options:
        options = json.loads(Path(s.adapter_options).read_text(encoding="utf-8"))
    return load_adapter(s.adapter, options)


def _http(s: Settings, method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(s.app + path, data=data, method=method, headers={
        "Authorization": "Bearer " + s.token, "X-Lanloom": "1", "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:     # the app on the same host; no other destination
        return json.loads(r.read().decode() or "{}")


def run(s: Settings, once: bool = False) -> int:
    if len(s.token) < 24:
        print("executor: LANLOOM_EXECUTOR_TOKEN must have at least 24 characters", file=sys.stderr)
        return 2
    if s.target == "controller" and not s.origins:
        print("executor: LANLOOM_EXECUTOR_ORIGINS is required for the controller target", file=sys.stderr)
        return 2
    s.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    adapter = build_adapter(s)
    keys = read_private(keys_path(s), [])

    def report(ev):
        try:
            _http(s, "POST", "/api/executor/report", ev)
        except Exception as exc:
            log.warning("report failed: %s", type(exc).__name__)

    runner = Runner(adapter, s.state, target=s.target, require_signature=True, keys=keys, origins=s.origins,
                    watch_s=s.watch_s, check_s=60, quiet_hours=s.quiet, report=report)
    recovered = runner.recover()
    if recovered:
        log.warning("recovered an interrupted run: %s", recovered.get("status"))
    info = {"target": s.target, "armed": s.armed(), "adapter": getattr(adapter, "label", "adapter"), "version": __version__,
            "keys": [k.get("fingerprint") for k in keys]}
    log.info("executor ready: target=%s armed=%s keys=%d", s.target, info["armed"], len(keys))
    while True:
        try:
            _http(s, "POST", "/api/executor/state", {"state": adapter.read(), "executor": info})
            for item in _http(s, "GET", "/api/executor/plans?target=" + s.target).get("plans") or []:
                runner.run(item.get("plan"), item.get("assertion"))
                _http(s, "POST", "/api/executor/state", {"state": adapter.read(), "executor": info})
        except Exception as exc:
            log.warning("cycle failed: %s", type(exc).__name__)
        if once:
            return 0
        time.sleep(POLL_S)


def trust_key(s: Settings, text: str) -> int:
    try:
        data = json.loads(text)
        key = data["keys"][0] if isinstance(data, dict) and "keys" in data else data
        entry = {k: str(key[k]) for k in ("id", "rp_id", "x", "y", "fingerprint")}
        int(entry["x"], 16), int(entry["y"], 16)
    except (ValueError, KeyError, TypeError, IndexError):
        print("trust-key: expected the JSON of one signing key as exported by the app", file=sys.stderr)
        return 2
    s.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    keys = [k for k in read_private(keys_path(s), []) if k.get("id") != entry["id"]] + [entry]
    write_private(keys_path(s), keys)
    print("pinned key %s for %s - compare this fingerprint with the one shown in the app" % (entry["fingerprint"], entry["rp_id"]))
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings()
    cmd = argv[0] if argv else "run"
    if cmd == "run":
        return run(s, once="--once" in argv)
    if cmd == "trust-key":
        return trust_key(s, sys.stdin.read())
    if cmd == "keys":
        for k in read_private(keys_path(s), []):
            print(k.get("fingerprint"), k.get("rp_id"))
        return 0
    if cmd == "status":
        print(json.dumps({"target": s.target, "armed": s.armed(), "keys": len(read_private(keys_path(s), [])),
                          "last": Runner(SimulatedController(settle_s=0), s.state, target=s.target).history(5)}, indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
