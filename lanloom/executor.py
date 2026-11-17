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
  LANLOOM_EXECUTOR_ADAPTER      family id of a built-in write adapter (target controller only); see
                                docs/COMPATIBILITY.md. Nothing outside the built-in list can be loaded.
  LANLOOM_EXECUTOR_ADAPTER_OPTIONS  path of the adapter's JSON options file with the write credentials:
                                mode 0600, owned by the executor's user, no symbolic link
  LANLOOM_EXECUTOR_ARM          must be exactly I-UNDERSTAND, and <state>/armed must exist, before real writes
  LANLOOM_EXECUTOR_DENY_HOSTS   comma-separated host names or addresses this executor must never connect to, e.g.
                                the real controller while it rehearses against a test twin (connection guard)
  LANLOOM_EXECUTOR_ORIGINS      comma-separated origins the app is opened from, e.g. https://lanloom.example
  LANLOOM_EXECUTOR_WATCH_S      watch time after a change (default 3600)
  LANLOOM_FW_QUIET_HOURS        e.g. 21-7: changes at night need an explicit confirmation
"""
import ipaddress
import json
import logging
import os
import socket
import sys
import time
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path

from . import __version__, integrations
from .updates import is_older as _older
from .firewall.adapter import SimulatedController, with_sample_hypervisor
from .firewall.runner import Runner, read_private, write_private
from .firewall.steps import PLAN_VERSION_MAX
from .integrations import capabilities as capmod
from .integrations.credentials import CredentialError, load_write_options

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
        self.deny = tuple(h.strip() for h in env.get("LANLOOM_EXECUTOR_DENY_HOSTS", "").split(",") if h.strip())

    def armed(self) -> bool:
        """Real writes need the phrase in the environment AND a file the operator created by hand."""
        return self.target == "controller" and self.arm == ARM_PHRASE and (self.state / "armed").is_file()


def keys_path(s: Settings) -> Path:
    return s.state / "signing-keys.json"


def build_adapter(s: Settings):
    if s.target == "simulation":
        saved = read_private(s.state / "simulation.json", None)
        if isinstance(saved, dict) and saved.get("devices"):
            saved = with_sample_hypervisor(saved)      # rehearsal states of earlier releases had no sample hypervisor
        return SimulatedController(state=saved, persist=lambda st: write_private(s.state / "simulation.json", st), settle_s=5,
                                   hypervisor=True)
    if s.target != "controller":
        raise SystemExit("LANLOOM_EXECUTOR_TARGET must be simulation or controller")
    if not s.armed():
        raise SystemExit("Real writes are not armed. Set LANLOOM_EXECUTOR_ARM=%s and create %s by hand "
                         "after reading docs/FIREWALL-EDITOR.md." % (ARM_PHRASE, s.state / "armed"))
    family = s.adapter
    if ":" in family or "." in family:
        raise SystemExit("LANLOOM_EXECUTOR_ADAPTER takes the id of a built-in adapter family (see docs/COMPATIBILITY.md); "
                         "loading adapter classes from elsewhere is not supported")
    try:
        manifest = integrations.get(family)
    except KeyError:
        raise SystemExit("unknown adapter family %r (built in: %s)" % (family, ", ".join(
            m.family for m in integrations.families() if m.write and m.family != "simulation") or "none yet"))
    if family == "simulation" or not manifest.write:
        raise SystemExit("family %r has no write adapter for the controller target "
                         "(use LANLOOM_EXECUTOR_TARGET=simulation to rehearse)" % family)
    options = {}
    if s.adapter_options:
        try:
            options = load_write_options(s.adapter_options, secret_fields=manifest.write_secret_fields)
        except CredentialError as exc:
            raise SystemExit("adapter options: %s" % exc)
    if "state_dir" in options:
        raise SystemExit("adapter options: state_dir is set by the executor, not in the options file")
    options["state_dir"] = str(s.state)          # e.g. the sign-in cap that holds across restarts
    try:
        adapter = integrations.load_write(family, **options)
    except (TypeError, ValueError) as exc:
        raise SystemExit("adapter options: %s" % exc)
    if hasattr(adapter, "use_identity"):         # lockout rules that need to know where the executor and the app run
        adapter.use_identity(lambda host: local_identity(host, s.app))
    return adapter


# ---------------------------------------------------------------------------- what the executor sees of itself
def _mac_hashes(mac) -> set:
    return integrations.guest_mac_hashes(mac)


def _read_lines(path: str) -> list:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(256 * 1024).splitlines()
    except OSError:
        return []


def local_identity(peer_host: str = None, app_url: str = None) -> tuple:
    """({address: reason}, {mac hash: reason}) the executor finds on its own: its own addresses and hardware addresses
    (host networking: the guest it runs on), the app's address, the name servers it uses and its default gateway,
    with the hardware addresses its neighbour table knows for them. Errors are ignored (fewer findings, never more)."""
    addrs, macs = {}, {}

    def add(ip, why):
        try:
            a = ipaddress.ip_address(str(ip).split("%", 1)[0])
        except ValueError:
            return
        if not a.is_loopback and not a.is_unspecified and str(a) not in addrs:
            addrs[str(a)] = why

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            add(info[4][0], "hv.why.executor")
    except OSError:
        pass
    if peer_host:
        try:
            fam = socket.AF_INET6 if ":" in peer_host else socket.AF_INET
            with socket.socket(fam, socket.SOCK_DGRAM) as s:      # no packet is sent: only the route is looked up
                s.connect((peer_host, 9))
                add(s.getsockname()[0], "hv.why.executor")
        except OSError:
            pass
    host = urlsplit(app_url or "").hostname if app_url else None
    if host:
        try:
            for info in socket.getaddrinfo(host, None):
                add(info[4][0], "hv.why.self")
        except OSError:
            pass
    for line in _read_lines("/etc/resolv.conf"):
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            add(parts[1], "hv.why.dns")
    for line in _read_lines("/proc/net/route")[1:]:
        parts = line.split()
        if len(parts) > 2 and parts[1] == "00000000" and parts[2] != "00000000":
            try:
                add(socket.inet_ntoa(bytes.fromhex(parts[2])[::-1]), "hv.why.gateway")
            except (ValueError, OSError):
                pass
    for name in sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else []:
        if name == "lo":
            continue
        for line in _read_lines("/sys/class/net/%s/address" % name)[:1]:
            for h in _mac_hashes(line.strip()):
                macs.setdefault(h, "hv.why.executor")
    for line in _read_lines("/proc/net/arp")[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[0] in addrs:
            for h in _mac_hashes(parts[3]):
                macs.setdefault(h, addrs[parts[0]])
    return addrs, macs


def guard_hosts(s: Settings, adapter) -> list:
    """The only hosts this executor may connect to: the app and the adapter's own endpoints."""
    from urllib.parse import urlsplit
    hosts = [urlsplit(s.app).hostname or "127.0.0.1"]
    try:
        hosts += [str(h) for h in (adapter.endpoints() if hasattr(adapter, "endpoints") else [])]
    except Exception:
        pass
    return hosts


def executor_info(s: Settings, adapter) -> dict:
    """What the executor tells the app about itself (no credentials, no addresses)."""
    caps = adapter.capabilities() if hasattr(adapter, "capabilities") else None
    try:
        caps = capmod.validate(caps) if caps else None
    except capmod.CapabilityError:
        caps = None
    return {"target": s.target, "armed": s.armed(), "adapter": getattr(adapter, "label", "adapter"), "version": __version__,
            "family": getattr(adapter, "family", None), "capabilities": capmod.public(caps) if caps else None,
            "plan_version_max": PLAN_VERSION_MAX, "api_level": integrations.INTEGRATION_API}




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
    from . import connguard as guard
    in_force = guard.install(guard_hosts(s, adapter), s.deny)
    if not in_force["allowed"]:
        print("executor: none of the app's or the controller's addresses could be resolved", file=sys.stderr)
        return 2
    log.info("connection guard: %d allowed address(es), %d refused", in_force["allowed"], in_force["denied"])
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
    info = dict(executor_info(s, adapter), keys=[k.get("fingerprint") for k in keys])
    log.info("executor ready: target=%s armed=%s keys=%d", s.target, info["armed"], len(keys))
    warned = set()
    while True:
        try:
            reply = _http(s, "POST", "/api/executor/state", {"state": adapter.read(), "executor": info})
            app = (reply or {}).get("app") if isinstance(reply, dict) else None
            if isinstance(app, dict) and app.get("version") and _older(__version__, app["version"]) and app["version"] not in warned:
                warned.add(app["version"])
                log.warning("this executor (%s) is older than the app (%s): update it by hand; plans of a newer "
                            "version are refused, never half-understood", __version__, app["version"])
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
    integrations.mark_executor_process()
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
                          "version": __version__, "plan_version_max": PLAN_VERSION_MAX, "api_level": integrations.INTEGRATION_API,
                          "last": Runner(SimulatedController(settle_s=0), s.state, target=s.target).history(5)}, indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
