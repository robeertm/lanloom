"""Update engine of the sidecar: request -> pull -> selftest -> probe -> backup -> swap -> health.

The app and the sidecar talk only through files on the shared data volume:

    <data>/update/request.json   written by the app   {"version","nonce","requested"[,"rollback"]}
    <data>/update/status.json    written by sidecar   {"state","version","from","message_key","log","nonce","t"}
    <data>/update/sidecar.json   written by sidecar   {"version","t"}  (heartbeat every 30 s)

Failures before the swap leave everything as it was (state "failed"). Failures after
the swap began restore the previous container and, if needed, the database
(state "rolled_back").
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import sidecar_version
from .runtimeapi import DEFAULT_SOCKET, RuntimeAPI, RuntimeAPIError, NotFound, Unreachable

# ------------------------------------------------------------------ constants
STATES = ("idle", "pulling", "selftest", "probe", "backup", "swap", "health",
          "done", "rolled_back", "failed")
TERMINAL = ("idle", "done", "rolled_back", "failed")

ROLE_LABEL = "org.lanloom.role"
ROLE_APP = "app"
ROLE_PROBE = "updater-probe"
PARENT_LABEL = "org.lanloom.updater.parent"
VERSION_LABEL = "org.opencontainers.image.version"

DB_NAME = "lanloom.db"
DB_SIDE_FILES = ("-wal", "-shm", "-journal")
PREVIOUS_TAG = "previous"
PREVIOUS_SUFFIX = "-previous"
KEEP_BACKUPS = 5
LOG_LINES = 50
LOG_LINE_MAX = 200
REQUEST_MAX_BYTES = 4096
SMALL_FILE_MAX = 16 * 1024 * 1024
SMALL_TOTAL_MAX = 64 * 1024 * 1024
DEFAULT_APP_PORT = 7380

SEMVER_RE = re.compile(r"\A(\d{1,6})\.(\d{1,6})\.(\d{1,6})\Z")
NONCE_RE = re.compile(r"\A[0-9a-f]{8,64}\Z")
REPO_RE = re.compile(r"\A[a-z0-9]+(?:[._-][a-z0-9]+)*(?::\d{1,5})?(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*\Z")
BACKUP_DIR_RE = re.compile(r"\A\d{8}T\d{6}Z-\d{1,6}\.\d{1,6}\.\d{1,6}(?:-\d+)?\Z")
SECRET_NAME_RE = re.compile(r"KEY|TOKEN|PASS|SECRET", re.IGNORECASE)

# i18n keys the app must translate (all prefixed "update.").
MESSAGE_KEYS = (
    "update.msg.done",
    "update.err.invalid_request",
    "update.err.invalid_version",
    "update.err.stale_request",
    "update.err.repository_not_allowed",
    "update.err.target_not_found",
    "update.err.target_ambiguous",
    "update.err.already_current",
    "update.err.downgrade_refused",
    "update.err.runtime_unreachable",
    "update.err.pull_failed",
    "update.err.version_label_mismatch",
    "update.err.selftest_failed",
    "update.err.probe_failed",
    "update.err.backup_failed",
    "update.err.swap_failed",
    "update.err.start_failed",
    "update.err.health_failed",
    "update.err.rollback_failed",
    "update.err.interrupted",
    "update.err.internal",
)


class UpdateError(Exception):
    """A refusal or failure with an i18n message key."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(detail or key)
        self.key = key
        self.detail = detail


# ------------------------------------------------------------------- helpers
def parse_semver(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    m = SEMVER_RE.match(value)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def split_ref(ref: str) -> tuple[str, str | None]:
    """'reg.example/a/b:1.2.3@sha256:..' -> ('reg.example/a/b', '1.2.3')."""
    ref = ref.split("@", 1)[0]
    slash, colon = ref.rfind("/"), ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1:]
    return ref, None


def is_qualified_repo(repo: str) -> bool:
    """True when the repository names a registry host (not a local-only name)."""
    if "/" not in repo:
        return False
    host = repo.split("/", 1)[0]
    return ("." in host or ":" in host) and not host.startswith("localhost")


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME_RE.search(name))


class Redactor:
    """Replaces values of secret-looking environment variables in text."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def learn_env(self, env: list[str] | None) -> None:
        for item in env or []:
            name, sep, value = item.partition("=")
            if sep and is_secret_name(name) and len(value) >= 4:
                self._values.add(value)

    def __call__(self, text: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            if value in text:
                text = text.replace(value, "***")
        return text


def _clean_line(text: str) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    text = " ".join(text.split())
    return text[:LOG_LINE_MAX]


def _owner_of(path: Path) -> tuple[int, int] | None:
    try:
        st = os.lstat(path)
        return st.st_uid, st.st_gid
    except OSError:
        return None


def _chown(path: Path, owner: tuple[int, int] | None) -> None:
    if owner is None or not hasattr(os, "lchown"):
        return
    try:
        os.lchown(path, owner[0], owner[1])
    except (PermissionError, OSError):
        pass


def chown_tree(root: Path, owner: tuple[int, int] | None) -> None:
    if owner is None:
        return
    _chown(root, owner)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            _chown(Path(dirpath) / name, owner)


def atomic_write_json(path: Path, obj: Any, owner: tuple[int, int] | None = None) -> None:
    """Write JSON via a unique temp file in the same directory + rename."""
    path = Path(path)
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        _chown(tmp, owner)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json_file(path: Path, max_bytes: int = REQUEST_MAX_BYTES) -> Any:
    """Read a small JSON file without following symlinks. Missing -> None."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UpdateError("update.err.invalid_request", f"cannot open request: {exc.strerror}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UpdateError("update.err.invalid_request", "request is not a regular file")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(max_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > max_bytes:
        raise UpdateError("update.err.invalid_request", "request too large")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UpdateError("update.err.invalid_request", "request is not valid JSON") from None


def sqlite_backup(src: Path, dst: Path) -> None:
    """Consistent online copy of a sqlite database (works while the app writes)."""
    tmp = dst.with_name(dst.name + ".part")
    if tmp.exists():
        tmp.unlink()
    source = sqlite3.connect(f"file:{urllib.parse.quote(str(src))}?mode=ro", uri=True, timeout=15)
    try:
        target = sqlite3.connect(str(tmp))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    os.replace(tmp, dst)


def fix_db_side_files(db: Path, owner: tuple[int, int] | None) -> None:
    """Opening a WAL database as root may create -wal/-shm owned by root; hand them back."""
    for suffix in DB_SIDE_FILES:
        side = db.with_name(db.name + suffix)
        if side.exists() and not side.is_symlink():
            _chown(side, owner)


def db_fingerprint(db: Path) -> str:
    h = hashlib.sha256()
    for path in (db, db.with_name(db.name + "-wal")):
        h.update(path.name.encode())
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except FileNotFoundError:
            h.update(b"<missing>")
    return h.hexdigest()


def copy_small_files(src: Path, dst: Path, skip_top: set[str]) -> int:
    """Copy regular files (no symlinks, no big files) from src into dst, recursively.

    Top-level names in skip_top are left out. Returns the number of bytes copied.
    """
    total = 0
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel = Path(dirpath).relative_to(src)
        if rel == Path("."):
            dirnames[:] = [d for d in dirnames if d not in skip_top]
            filenames = [f for f in filenames if f not in skip_top]
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size > SMALL_FILE_MAX:
                continue
            if total + st.st_size > SMALL_TOTAL_MAX:
                return total
            target = dst / rel / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target, follow_symlinks=False)
            total += st.st_size
    return total


def http_health(url: str, timeout: float = 3.0) -> dict | None:
    """GET a health URL (no redirects, size-capped). Returns the JSON object on 200."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        return None
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        conn.request("GET", parts.path or "/", headers={"Accept": "application/json"})
        resp = conn.getresponse()
        data = resp.read(65537)
        if resp.status != 200 or len(data) > 65536:
            return None
        obj = json.loads(data)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def env_list_to_dict(env: list[str] | None) -> dict[str, str]:
    out = {}
    for item in env or []:
        name, sep, value = item.partition("=")
        if sep:
            out[name] = value
    return out


def _utc_stamp(ts: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts))


# ------------------------------------------------------------------ settings
def _env_float(env: dict, name: str, default: float) -> float:
    try:
        return float(env.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    data_dir: Path = Path("/data")
    socket_path: str = DEFAULT_SOCKET
    target_name: str | None = None
    allow_local: bool = False          # tests/bench only: accept local repository names, no pull
    health_url: str | None = None      # tests/bench only: override the app health URL
    poll_s: float = 2.0
    heartbeat_s: float = 30.0
    pull_timeout: float = 900.0
    selftest_timeout: float = 60.0
    probe_timeout: float = 90.0
    health_timeout: float = 120.0
    rollback_health_timeout: float = 120.0
    stop_timeout: int = 20
    settle_s: float = 5.0
    check_interval: float = 1.0
    stale_request_s: float = 3600.0
    probe_ports: tuple[int, int] = (17380, 17399)
    data_mount: str = "/data"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        s = cls()
        s.data_dir = Path(env.get("LANLOOM_DATA") or "/data")
        s.socket_path = env.get("LANLOOM_RUNTIME_SOCKET") or DEFAULT_SOCKET
        s.target_name = env.get("LANLOOM_TARGET") or None
        s.allow_local = env.get("LANLOOM_UPDATER_ALLOW_LOCAL") == "1"
        s.health_url = env.get("LANLOOM_HEALTH_URL") or None
        s.poll_s = max(0.2, _env_float(env, "LANLOOM_UPDATER_POLL", 2.0))
        return s


# ------------------------------------------------------------------- updater
@dataclass
class _Run:
    """Mutable bookkeeping of one update run."""
    nonce: str
    version: str
    target: dict = field(default_factory=dict)
    name: str = ""
    repo: str = ""
    original_tag: str | None = None
    current: str | None = None
    candidate: str = ""
    backup_dir: Path | None = None
    baseline: str | None = None
    new_id: str | None = None
    swap_started: bool = False


class Updater:
    def __init__(self, api: RuntimeAPI, settings: Settings,
                 health_fn: Callable[[str], dict | None] = http_health,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 echo: Callable[[str], None] | None = None):
        self.api = api
        self.s = settings
        self.health_fn = health_fn
        self.clock = clock
        self.sleep = sleep
        self.echo = echo if echo is not None else (lambda line: print(line, flush=True))
        self.redact = Redactor()
        self.status: dict = {"state": "idle", "version": None, "from": None, "message_key": None,
                             "log": [], "nonce": None, "t": 0}
        self._stop = threading.Event()
        self._hb_thread: threading.Thread | None = None

    # ------------------------------------------------------------- paths
    @property
    def update_dir(self) -> Path:
        return self.s.data_dir / "update"

    @property
    def request_path(self) -> Path:
        return self.update_dir / "request.json"

    @property
    def status_path(self) -> Path:
        return self.update_dir / "status.json"

    @property
    def heartbeat_path(self) -> Path:
        return self.update_dir / "sidecar.json"

    @property
    def backups_dir(self) -> Path:
        return self.s.data_dir / "backups"

    @property
    def db_path(self) -> Path:
        return self.s.data_dir / DB_NAME

    def data_owner(self) -> tuple[int, int] | None:
        """Owner of the app's data (the database if present, else the data dir)."""
        return _owner_of(self.db_path) if self.db_path.exists() else _owner_of(self.s.data_dir)

    def ensure_update_dir(self) -> None:
        d = self.update_dir
        if d.is_symlink():
            raise UpdateError("update.err.internal", "update dir is a symlink")
        if not d.exists():
            d.mkdir(mode=0o755, parents=True)
            _chown(d, self.data_owner())

    # ------------------------------------------------------------ status
    def log(self, line: str) -> None:
        line = _clean_line(self.redact(str(line)))
        self.status["log"] = (self.status.get("log") or [])[-(LOG_LINES - 1):] + [line]
        self.echo(f"updater: {line}")
        self.write_status()

    def set_state(self, state: str, message_key: str | None = None, log: str | None = None) -> None:
        assert state in STATES, state
        self.status["state"] = state
        if message_key is not None:
            self.status["message_key"] = message_key
        if log:
            self.log(log)
        else:
            self.write_status()

    def write_status(self) -> None:
        self.status["t"] = round(self.clock(), 3)
        try:
            self.ensure_update_dir()
            atomic_write_json(self.status_path, self.status, self.data_owner())
        except (OSError, UpdateError) as exc:
            self.echo(f"updater: cannot write status: {type(exc).__name__}")

    def write_heartbeat(self) -> None:
        try:
            self.ensure_update_dir()
            atomic_write_json(self.heartbeat_path, {"version": sidecar_version(), "t": round(self.clock(), 3)},
                              self.data_owner())
        except (OSError, UpdateError) as exc:
            self.echo(f"updater: cannot write heartbeat: {type(exc).__name__}")

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            self.write_heartbeat()
            self._stop.wait(self.s.heartbeat_s)

    def start_heartbeat(self) -> None:
        if self._hb_thread is None:
            self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
            self._hb_thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ----------------------------------------------------------- request
    def take_request(self) -> dict | None:
        """Read and remove a pending request. Returns None if there is none."""
        if not self.request_path.exists() and not self.request_path.is_symlink():
            return None
        try:
            req = read_json_file(self.request_path)
        finally:
            try:
                os.unlink(self.request_path)
            except FileNotFoundError:
                pass
        if req is None:
            return None
        if not isinstance(req, dict):
            raise UpdateError("update.err.invalid_request", "request is not an object")
        return req

    def validate_request(self, req: dict) -> tuple[str, str, bool, str | None]:
        nonce = req.get("nonce")
        if not isinstance(nonce, str) or not NONCE_RE.match(nonce):
            raise UpdateError("update.err.invalid_request", "missing or malformed nonce")
        version = req.get("version")
        if parse_semver(version) is None:
            raise UpdateError("update.err.invalid_version", "version must be MAJOR.MINOR.PATCH")
        requested = req.get("requested")
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise UpdateError("update.err.invalid_request", "missing request time")
        now = self.clock()
        if requested < now - self.s.stale_request_s or requested > now + 300:
            raise UpdateError("update.err.stale_request", "request is too old or from the future")
        rollback = req.get("rollback", False)
        if not isinstance(rollback, bool):
            raise UpdateError("update.err.invalid_request", "rollback must be true or false")
        repository = req.get("repository")
        if repository is not None and not isinstance(repository, str):
            raise UpdateError("update.err.invalid_request", "repository must be a string")
        return version, nonce, rollback, repository

    # ----------------------------------------------------------- target
    def find_target(self) -> dict:
        try:
            if self.s.target_name:
                try:
                    return self.api.container_inspect(self.s.target_name)
                except NotFound:
                    raise UpdateError("update.err.target_not_found", "target container not found") from None
            found = self.api.containers(labels=[f"{ROLE_LABEL}={ROLE_APP}"])
            cands = [c for c in found
                     if not any(n.lstrip("/").endswith(PREVIOUS_SUFFIX) for n in c.get("Names") or [])]
            if len(cands) > 1:
                running = [c for c in cands if c.get("State") == "running"]
                cands = running if running else cands
            if not cands:
                raise UpdateError("update.err.target_not_found", "no container labelled as the app")
            if len(cands) > 1:
                raise UpdateError("update.err.target_ambiguous", "several app containers found")
            return self.api.container_inspect(cands[0]["Id"])
        except Unreachable:
            raise UpdateError("update.err.runtime_unreachable", "container runtime not reachable") from None

    @staticmethod
    def is_own_container(container: dict) -> bool:
        """The runtime sets the hostname to the short container id by default."""
        host = socket.gethostname()
        return len(host) >= 12 and (container.get("Id") or "").startswith(host)

    def app_port(self, container: dict) -> int:
        env = env_list_to_dict((container.get("Config") or {}).get("Env"))
        try:
            port = int(env.get("LANLOOM_PORT", DEFAULT_APP_PORT))
            return port if 0 < port < 65536 else DEFAULT_APP_PORT
        except ValueError:
            return DEFAULT_APP_PORT

    @staticmethod
    def network_mode(container: dict) -> str:
        return ((container.get("HostConfig") or {}).get("NetworkMode")) or "default"

    @staticmethod
    def container_ip(container: dict) -> str | None:
        ns = container.get("NetworkSettings") or {}
        for net in (ns.get("Networks") or {}).values():
            ip = (net or {}).get("IPAddress")
            if ip:
                return ip
        return ns.get("IPAddress") or None

    def health_url_for(self, container: dict, port: int, override: str | None = None) -> str | None:
        if override:
            return override
        if self.network_mode(container) == "host":
            return f"http://127.0.0.1:{port}/api/health"
        ip = self.container_ip(container)
        if not ip:
            return None
        host = f"[{ip}]" if ":" in ip else ip
        return f"http://{host}:{port}/api/health"

    def current_version(self, container: dict) -> str | None:
        state = container.get("State") or {}
        if state.get("Running"):
            url = self.health_url_for(container, self.app_port(container), self.s.health_url)
            if url:
                h = self.health_fn(url)
                if h and parse_semver(h.get("version")):
                    return h["version"]
        img = self.api.image_inspect(container.get("Image") or "") or {}
        label = ((img.get("Config") or {}).get("Labels") or {}).get(VERSION_LABEL)
        if parse_semver(label):
            return label
        _, tag = split_ref((container.get("Config") or {}).get("Image") or "")
        return tag if parse_semver(tag) else None

    # -------------------------------------------------------------- main
    def handle_request(self, req: dict | None) -> dict:
        """Validate and perform one request. Always ends in a terminal state."""
        self.status = {"state": "pulling", "version": None, "from": None, "message_key": None,
                       "log": [], "nonce": None, "t": 0}
        try:
            if req is None:
                raise UpdateError("update.err.invalid_request", "empty request")
            nonce = req.get("nonce") if isinstance(req, dict) else None
            if isinstance(nonce, str) and NONCE_RE.match(nonce):
                self.status["nonce"] = nonce
            version = req.get("version") if isinstance(req, dict) else None
            if isinstance(version, str) and parse_semver(version):
                self.status["version"] = version
            version, nonce, rollback, repository = self.validate_request(req)
            self.perform(version, nonce, rollback, repository)
        except UpdateError as exc:
            self.set_state("failed", exc.key, f"failed: {exc.detail or exc.key}")
        except Exception as exc:  # pragma: no cover - last resort, never leak details
            self.set_state("failed", "update.err.internal", f"internal error: {type(exc).__name__}")
        return self.status

    def perform(self, version: str, nonce: str, rollback: bool, repository: str | None) -> None:
        run = _Run(nonce=nonce, version=version)
        self.set_state("pulling", None, f"request {version}")
        run.target = self.find_target()
        if self.is_own_container(run.target):
            raise UpdateError("update.err.target_not_found", "target is the sidecar itself")
        self.redact.learn_env((run.target.get("Config") or {}).get("Env"))
        run.name = (run.target.get("Name") or "").lstrip("/")
        run.repo, run.original_tag = split_ref((run.target.get("Config") or {}).get("Image") or "")
        self.check_repository(run.repo, repository)
        run.current = self.current_version(run.target)
        self.status["from"] = run.current
        if run.current and parse_semver(run.current):
            if parse_semver(version) == parse_semver(run.current):
                raise UpdateError("update.err.already_current", f"{version} is already running")
            if parse_semver(version) < parse_semver(run.current) and not rollback:  # type: ignore[operator]
                raise UpdateError("update.err.downgrade_refused", f"{version} is older than {run.current}")
        run.candidate = f"{run.repo}:{version}"
        self.log(f"target {run.name} runs {run.current or 'unknown'}")

        self.step_pull(run)
        self.set_state("selftest", None, "selftest of candidate")
        self.step_selftest(run)
        self.set_state("probe", None, "probe start with a copy of the data")
        self.step_probe(run)
        self.set_state("backup", None, "backup of image and database")
        self.step_backup(run)
        self.set_state("swap", None, f"swap {run.current or 'unknown'} -> {version}")
        self.step_swap_and_health(run)

    def check_repository(self, repo: str, requested: str | None) -> None:
        if not repo or not REPO_RE.match(repo):
            raise UpdateError("update.err.repository_not_allowed", "target image reference is not usable")
        if requested is not None and requested != repo:
            raise UpdateError("update.err.repository_not_allowed", "only the repository of the running image")
        if not is_qualified_repo(repo) and not self.s.allow_local:
            raise UpdateError("update.err.repository_not_allowed", "repository without registry host")

    # -------------------------------------------------------------- pull
    def step_pull(self, run: _Run) -> None:
        local = self.api.image_inspect(run.candidate)
        if self.s.allow_local and not is_qualified_repo(run.repo):
            if not local:
                raise UpdateError("update.err.pull_failed", "local test image not present")
            self.log(f"local test image {run.candidate} (no pull)")
        else:
            self.log(f"pull {run.candidate}")
            seen: dict[str, float] = {"t": 0.0}

            def progress(obj: dict) -> None:
                now = time.monotonic()
                if now - seen["t"] > 10 and obj.get("status"):
                    seen["t"] = now
                    self.log(f"pull: {obj.get('status')}")

            try:
                self.api.image_pull(run.repo, run.version, progress, deadline_s=self.s.pull_timeout)
            except RuntimeAPIError as exc:
                raise UpdateError("update.err.pull_failed", f"pull failed: {exc.message}") from None
            local = self.api.image_inspect(run.candidate)
            if not local:
                raise UpdateError("update.err.pull_failed", "image missing after pull")
        label = ((local.get("Config") or {}).get("Labels") or {}).get(VERSION_LABEL)
        if label and label != run.version:
            raise UpdateError("update.err.version_label_mismatch", f"image says {label}")
        self.log("candidate image present")

    # ---------------------------------------------------------- selftest
    def _throwaway_labels(self, run: _Run) -> dict:
        return {ROLE_LABEL: ROLE_PROBE, PARENT_LABEL: run.name}

    def _wait_exit(self, cid: str, timeout: float) -> int | None:
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            st = (self.api.container_inspect(cid).get("State") or {})
            if st.get("Status") in ("exited", "dead"):
                return int(st.get("ExitCode", -1))
            self.sleep(min(self.s.check_interval, 0.5))
        return None

    def _tail_logs(self, cid: str, prefix: str, n: int = 8) -> None:
        try:
            for line in self.api.container_logs(cid, tail=n):
                self.log(f"{prefix}: {line}")
        except RuntimeAPIError:
            pass

    def _remove_quietly(self, cid: str | None) -> None:
        if not cid:
            return
        try:
            self.api.container_remove(cid, force=True)
        except RuntimeAPIError:
            pass

    def step_selftest(self, run: _Run) -> None:
        cfg = run.target.get("Config") or {}
        body = {
            "Image": run.candidate,
            "Cmd": ["python", "-m", "lanloom", "--selftest"],
            "Env": ["LANLOOM_UPDATE_CHECK=0", "LANLOOM_PROBE=1", "LANLOOM_DATA=/tmp/selftest-data"],
            "Labels": self._throwaway_labels(run),
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True,
                           "Tmpfs": {"/tmp": "rw,size=64m"}, "RestartPolicy": {"Name": "no"}},
        }
        if cfg.get("User"):
            body["User"] = cfg["User"]
        cid = None
        try:
            cid = self.api.container_create(f"{run.name}-selftest-{run.nonce[:8]}", body)
            self.api.container_start(cid)
            code = self._wait_exit(cid, self.s.selftest_timeout)
            self._tail_logs(cid, "selftest")
            if code is None:
                raise UpdateError("update.err.selftest_failed", "selftest timed out")
            if code != 0:
                raise UpdateError("update.err.selftest_failed", f"selftest exit code {code}")
            self.log("selftest passed")
        except RuntimeAPIError as exc:
            raise UpdateError("update.err.selftest_failed", f"selftest: {exc.message}") from None
        finally:
            self._remove_quietly(cid)

    # ------------------------------------------------------------- probe
    def pick_probe_port(self, host_mode: bool) -> int:
        lo, hi = self.s.probe_ports
        if not host_mode:
            return lo
        for port in range(lo, hi + 1):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise UpdateError("update.err.probe_failed", "no free probe port")

    def data_mount(self, container: dict) -> dict | None:
        for m in container.get("Mounts") or []:
            if m.get("Destination") == self.s.data_mount:
                return m
        return None

    def probe_mount_spec(self, container: dict, subdir: str) -> dict:
        """HostConfig fragment that mounts <data>/<subdir> of the app's data volume at /data."""
        m = self.data_mount(container)
        if not m:
            raise UpdateError("update.err.probe_failed", "app has no data mount")
        if m.get("Type") == "volume" and m.get("Name") and self.api.api_at_least(1, 45):
            return {"Mounts": [{"Type": "volume", "Source": m["Name"], "Target": self.s.data_mount,
                                "VolumeOptions": {"Subpath": subdir}}]}
        source = m.get("Source")
        if not source:
            raise UpdateError("update.err.probe_failed", "data mount has no source")
        return {"Binds": [f"{source.rstrip('/')}/{subdir}:{self.s.data_mount}"]}

    def prepare_probe_data(self, probe_dir: Path) -> None:
        if probe_dir.exists() or probe_dir.is_symlink():
            self._rmtree(probe_dir)
        probe_dir.mkdir(mode=0o755)
        owner = self.data_owner()
        if self.db_path.exists():
            sqlite_backup(self.db_path, probe_dir / DB_NAME)
            fix_db_side_files(self.db_path, owner)
        skip = {"backups", "update", DB_NAME} | {DB_NAME + s for s in DB_SIDE_FILES}
        copy_small_files(self.s.data_dir, probe_dir, skip)
        chown_tree(probe_dir, owner)

    @staticmethod
    def _rmtree(path: Path) -> None:
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def user_env(self, container: dict) -> list[str]:
        """Env entries set on the container that do not come from its image."""
        img = self.api.image_inspect(container.get("Image") or "") or {}
        image_env = set((img.get("Config") or {}).get("Env") or [])
        return [e for e in (container.get("Config") or {}).get("Env") or [] if e not in image_env]

    def step_probe(self, run: _Run) -> None:
        mode = self.network_mode(run.target)
        host_mode = mode == "host"
        port = self.pick_probe_port(host_mode)
        sub = f"update/probe-{run.nonce}"
        probe_dir = self.s.data_dir / sub
        override = {"LANLOOM_PORT": str(port), "LANLOOM_HOST": "127.0.0.1" if host_mode else "0.0.0.0",
                    "LANLOOM_UPDATE_CHECK": "0", "LANLOOM_DATA": self.s.data_mount, "LANLOOM_PROBE": "1"}
        env = [e for e in self.user_env(run.target) if e.partition("=")[0] not in override]
        env += [f"{k}={v}" for k, v in override.items()]
        cfg = run.target.get("Config") or {}
        cid = None
        try:
            try:
                self.prepare_probe_data(probe_dir)
            except (OSError, sqlite3.Error) as exc:
                raise UpdateError("update.err.probe_failed", f"cannot copy data: {type(exc).__name__}") from None
            host_cfg = {"NetworkMode": mode, "ReadonlyRootfs": True, "Tmpfs": {"/tmp": "rw,size=64m"},
                        "RestartPolicy": {"Name": "no"}}
            host_cfg.update(self.probe_mount_spec(run.target, sub))
            body = {"Image": run.candidate, "Env": env, "Labels": self._throwaway_labels(run),
                    "HostConfig": host_cfg}
            if cfg.get("User"):
                body["User"] = cfg["User"]
            cid = self.api.container_create(f"{run.name}-probe-{run.nonce[:8]}", body)
            self.api.container_start(cid)
            self.log(f"probe listens on port {port}")
            deadline = self.clock() + self.s.probe_timeout
            last = None
            while self.clock() < deadline:
                info = self.api.container_inspect(cid)
                st = info.get("State") or {}
                if st.get("Status") in ("exited", "dead"):
                    self._tail_logs(cid, "probe")
                    raise UpdateError("update.err.probe_failed", f"probe exited with code {st.get('ExitCode')}")
                url = self.health_url_for(info, port)
                h = self.health_fn(url) if url else None
                if h is not None:
                    last = h.get("version")
                    if last == run.version:
                        self.log("probe healthy")
                        return
                self.sleep(self.s.check_interval)
            self._tail_logs(cid, "probe")
            raise UpdateError("update.err.probe_failed",
                              f"probe not healthy in time (last version {last or 'none'})")
        except RuntimeAPIError as exc:
            raise UpdateError("update.err.probe_failed", f"probe: {exc.message}") from None
        finally:
            self._remove_quietly(cid)
            self._rmtree(probe_dir)

    # ------------------------------------------------------------ backup
    def backup_data(self, dest: Path) -> None:
        """Consistent copy of the database plus small config files into dest."""
        owner = self.data_owner()
        dest.mkdir(mode=0o750, parents=False, exist_ok=True)
        if self.db_path.exists():
            sqlite_backup(self.db_path, dest / DB_NAME)
            fix_db_side_files(self.db_path, owner)
        skip = {"backups", "update", DB_NAME} | {DB_NAME + s for s in DB_SIDE_FILES}
        copy_small_files(self.s.data_dir, dest, skip)
        chown_tree(dest, owner)

    def prune_backups(self, keep: int = KEEP_BACKUPS) -> list[str]:
        if not self.backups_dir.is_dir() or self.backups_dir.is_symlink():
            return []
        entries = sorted(p.name for p in self.backups_dir.iterdir()
                         if BACKUP_DIR_RE.match(p.name) and p.is_dir() and not p.is_symlink())
        removed = entries[:-keep] if len(entries) > keep else []
        for name in removed:
            shutil.rmtree(self.backups_dir / name, ignore_errors=True)
        return removed

    def step_backup(self, run: _Run) -> None:
        try:
            if run.target.get("Image"):
                self.api.image_tag(run.target["Image"], run.repo, PREVIOUS_TAG)
                self.log(f"image of {run.current or 'unknown'} tagged as {PREVIOUS_TAG}")
            if self.backups_dir.is_symlink():
                raise UpdateError("update.err.backup_failed", "backups dir is a symlink")
            if not self.backups_dir.exists():
                self.backups_dir.mkdir(mode=0o750)
                _chown(self.backups_dir, self.data_owner())
            base = f"{_utc_stamp(self.clock())}-{run.current or '0.0.0'}"
            dest = self.backups_dir / base
            n = 1
            while dest.exists():
                dest = self.backups_dir / f"{base}-{n}"
                n += 1
            self.backup_data(dest)
            atomic_write_json(dest / "backup.json", {"from": run.current, "to": run.version,
                                                     "image": run.target.get("Image"), "t": self.clock()},
                              self.data_owner())
            run.backup_dir = dest
            removed = self.prune_backups()
            self.log(f"backup {dest.name}" + (f", pruned {len(removed)}" if removed else ""))
        except UpdateError:
            raise
        except (OSError, sqlite3.Error, RuntimeAPIError) as exc:
            raise UpdateError("update.err.backup_failed", f"backup failed: {type(exc).__name__}") from None

    # -------------------------------------------------------------- swap
    def build_create_body(self, container: dict, image: str) -> tuple[dict, list[tuple[str, dict]]]:
        """Clone a container's create parameters for a new image.

        Values identical to the old image's defaults are left out so the new
        image's defaults apply (environment, labels such as the version label,
        command, entrypoint, workdir, healthcheck ...).
        """
        cfg = dict(container.get("Config") or {})
        img = self.api.image_inspect(container.get("Image") or "") or {}
        icfg = img.get("Config") or {}
        mode = self.network_mode(container)
        body: dict[str, Any] = {}
        passthrough = ("Tty", "OpenStdin", "StdinOnce", "AttachStdin", "AttachStdout",
                       "AttachStderr", "StopTimeout")
        for key in passthrough:
            if key in cfg and cfg[key] not in (None, ""):
                body[key] = cfg[key]
        for key in ("User", "Cmd", "Entrypoint", "WorkingDir", "Healthcheck", "StopSignal", "ExposedPorts",
                    "Volumes", "OnBuild", "Shell"):
            value = cfg.get(key)
            if value not in (None, "", [], {}) and value != icfg.get(key):
                body[key] = value
        image_env = set(icfg.get("Env") or [])
        body["Env"] = [e for e in cfg.get("Env") or [] if e not in image_env]
        image_labels = icfg.get("Labels") or {}
        body["Labels"] = {k: v for k, v in (cfg.get("Labels") or {}).items() if image_labels.get(k) != v}
        hostname = cfg.get("Hostname")
        cid = container.get("Id") or ""
        if hostname and mode not in ("host",) and not mode.startswith("container:") \
                and not cid.startswith(hostname):
            body["Hostname"] = hostname
            if cfg.get("Domainname"):
                body["Domainname"] = cfg["Domainname"]
        body["Image"] = image
        body["HostConfig"] = dict(container.get("HostConfig") or {})

        networks: list[tuple[str, dict]] = []
        if mode not in ("host", "none") and not mode.startswith("container:"):
            for net_name, ep in ((container.get("NetworkSettings") or {}).get("Networks") or {}).items():
                ep = ep or {}
                clean: dict[str, Any] = {}
                aliases = [a for a in ep.get("Aliases") or [] if not cid.startswith(a)]
                if aliases:
                    clean["Aliases"] = aliases
                for key in ("IPAMConfig", "Links", "DriverOpts"):
                    if ep.get(key):
                        clean[key] = ep[key]
                networks.append((net_name, clean))
        primary = [n for n in networks if n[0] == mode] or networks[:1]
        extra = [n for n in networks if n not in primary]
        if primary:
            if self.api.api_at_least(1, 44):
                body["NetworkingConfig"] = {"EndpointsConfig": {n: ep for n, ep in networks}}
                extra = []
            else:
                body["NetworkingConfig"] = {"EndpointsConfig": {primary[0][0]: primary[0][1]}}
        return body, extra

    def wait_app_healthy(self, cid: str, version: str, timeout: float) -> tuple[bool, str]:
        deadline = self.clock() + timeout
        healthy_since = None
        reason = "not healthy in time"
        while self.clock() < deadline:
            try:
                info = self.api.container_inspect(cid)
            except RuntimeAPIError as exc:
                return False, f"inspect failed: {exc.message}"
            st = info.get("State") or {}
            if int(info.get("RestartCount") or 0) > 0 or st.get("Restarting"):
                return False, "container restarted"
            if st.get("Status") in ("exited", "dead"):
                return False, f"container exited with code {st.get('ExitCode')}"
            url = self.health_url_for(info, self.app_port(info), self.s.health_url)
            h = self.health_fn(url) if url else None
            if h is not None and h.get("version") == version:
                if healthy_since is None:
                    healthy_since = self.clock()
                if self.clock() - healthy_since >= self.s.settle_s:
                    return True, "healthy"
            else:
                if h is not None:
                    reason = f"health reports {h.get('version')}"
                healthy_since = None
            self.sleep(self.s.check_interval)
        return False, reason

    def step_swap_and_health(self, run: _Run) -> None:
        old_id = run.target["Id"]
        prev_name = run.name + PREVIOUS_SUFFIX
        try:
            body, extra = self.build_create_body(run.target, run.candidate)
            try:
                stale = self.api.container_inspect(prev_name)
                if stale.get("Id") != old_id:
                    self.api.container_remove(stale["Id"], force=True)
                    self.log(f"removed stale {prev_name}")
            except NotFound:
                pass
            self.api.container_rename(old_id, prev_name)
        except RuntimeAPIError as exc:
            raise UpdateError("update.err.swap_failed", f"swap not started: {exc.message}") from None
        run.swap_started = True
        try:
            self.api.container_stop(old_id, self.s.stop_timeout)
            self.log(f"stopped {run.current or 'old version'}")
            self.final_backup(run)
            run.new_id = self.api.container_create(run.name, body)
            for net_name, ep in extra:
                self.api.network_connect(net_name, run.new_id, ep)
            self.api.container_start(run.new_id)
        except RuntimeAPIError as exc:
            self.rollback(run, "update.err.start_failed", f"start failed: {exc.message}")
            return
        self.set_state("health", None, f"waiting for {run.version} to become healthy")
        ok, reason = self.wait_app_healthy(run.new_id, run.version, self.s.health_timeout)
        if not ok:
            self._tail_logs(run.new_id, "new")
            self.rollback(run, "update.err.health_failed", reason)
            return
        self._remove_quietly(old_id)
        if run.original_tag and run.original_tag != run.version and not parse_semver(run.original_tag):
            # Keep a moving tag (e.g. "latest") pointing at what now runs, so a later
            # compose "up" does not recreate the container with the old image.
            try:
                self.api.image_tag(run.candidate, run.repo, run.original_tag)
            except RuntimeAPIError:
                self.log(f"could not move tag {run.original_tag}")
        self.set_state("done", "update.msg.done", f"updated {run.current or 'unknown'} -> {run.version}")

    def final_backup(self, run: _Run) -> None:
        """With the old app stopped the database is quiescent: refresh the backup copy."""
        if not self.db_path.exists():
            run.baseline = db_fingerprint(self.db_path)
            return
        try:
            if run.backup_dir is not None:
                sqlite_backup(self.db_path, run.backup_dir / DB_NAME)
                fix_db_side_files(self.db_path, self.data_owner())
                _chown(run.backup_dir / DB_NAME, self.data_owner())
        except (OSError, sqlite3.Error):
            self.log("refreshing the backup failed, keeping the first copy")
        run.baseline = db_fingerprint(self.db_path)

    def restore_db_if_changed(self, run: _Run) -> None:
        if run.baseline is None or run.backup_dir is None:
            return
        if db_fingerprint(self.db_path) == run.baseline:
            self.log("database unchanged")
            return
        src = run.backup_dir / DB_NAME
        if not src.exists():
            self.log("database changed but no backup copy exists")
            return
        owner = self.data_owner()
        mode = None
        try:
            mode = stat.S_IMODE(os.stat(self.db_path).st_mode)
        except OSError:
            pass
        tmp = self.db_path.with_name(DB_NAME + ".restore")
        shutil.copyfile(src, tmp)
        if mode is not None:
            os.chmod(tmp, mode)
        _chown(tmp, owner)
        for suffix in DB_SIDE_FILES:
            side = self.db_path.with_name(DB_NAME + suffix)
            if side.exists() or side.is_symlink():
                side.unlink()
        os.replace(tmp, self.db_path)
        self.log(f"database restored from {run.backup_dir.name}")

    def rollback(self, run: _Run, key: str, reason: str) -> None:
        self.log(f"{reason}; rolling back to {run.current or 'previous version'}")
        old_id = run.target["Id"]
        problems = []
        if run.new_id:
            try:
                self.api.container_stop(run.new_id, 10)
            except RuntimeAPIError:
                pass
            self._remove_quietly(run.new_id)
        try:
            other = self.api.container_inspect(run.name)
            if other.get("Id") != old_id:
                self.api.container_remove(other["Id"], force=True)
        except NotFound:
            pass
        except RuntimeAPIError as exc:
            problems.append(f"cleanup: {exc.message}")
        try:
            self.restore_db_if_changed(run)
        except (OSError, sqlite3.Error) as exc:
            problems.append(f"restore: {type(exc).__name__}")
        try:
            self.api.container_rename(old_id, run.name)
        except RuntimeAPIError as exc:
            problems.append(f"rename: {exc.message}")
        try:
            self.api.container_start(old_id)
        except RuntimeAPIError as exc:
            problems.append(f"start: {exc.message}")
        version = run.current
        if version:
            ok, why = self.wait_app_healthy(old_id, version, self.s.rollback_health_timeout)
        else:
            ok, why = self._running_without_restarts(old_id), "state check"
        if ok and not problems:
            self.set_state("rolled_back", key, f"rolled back, {run.current or 'previous version'} runs again")
            return
        for p in problems:
            self.log(p)
        raise UpdateError("update.err.rollback_failed", f"rollback incomplete: {why}")

    def _running_without_restarts(self, cid: str) -> bool:
        try:
            info = self.api.container_inspect(cid)
        except RuntimeAPIError:
            return False
        return bool((info.get("State") or {}).get("Running")) and int(info.get("RestartCount") or 0) == 0

    # ---------------------------------------------------------- recovery
    def recover(self) -> None:
        """Clean up after an interrupted run (sidecar restarted mid-update)."""
        labels = [f"{ROLE_LABEL}={ROLE_PROBE}"]
        if self.s.target_name:
            labels.append(f"{PARENT_LABEL}={self.s.target_name}")
        try:
            for c in self.api.containers(labels=labels):
                self._remove_quietly(c.get("Id"))
        except RuntimeAPIError:
            pass
        if self.update_dir.is_dir():
            for p in self.update_dir.glob("probe-*"):
                self._rmtree(p)
        try:
            previous = read_json_file(self.status_path, 256 * 1024)
        except UpdateError:
            previous = None
        if isinstance(previous, dict):
            self.status.update({k: previous.get(k) for k in self.status if k in previous})
            if not isinstance(self.status.get("log"), list):
                self.status["log"] = []
        if self.status.get("state") in TERMINAL:
            if not isinstance(previous, dict):
                self.write_status()
            return
        self.log("previous run was interrupted")
        try:
            self._recover_containers()
        except (RuntimeAPIError, UpdateError) as exc:
            self.log(f"recovery: {getattr(exc, 'detail', '') or type(exc).__name__}")
        self.set_state("failed", "update.err.interrupted")

    def _recover_containers(self) -> None:
        if self.s.target_name:
            name = self.s.target_name
        else:
            found = self.api.containers(labels=[f"{ROLE_LABEL}={ROLE_APP}"])
            names = sorted({n.lstrip("/") for c in found for n in c.get("Names") or []})
            bases = {n[:-len(PREVIOUS_SUFFIX)] for n in names if n.endswith(PREVIOUS_SUFFIX)}
            if len(bases) != 1:
                return
            name = bases.pop()
        try:
            prev = self.api.container_inspect(name + PREVIOUS_SUFFIX)
        except NotFound:
            return
        try:
            cur = self.api.container_inspect(name)
        except NotFound:
            cur = None
        if cur and (cur.get("State") or {}).get("Running"):
            self.log(f"{name} is running; kept {name}{PREVIOUS_SUFFIX} for inspection")
            return
        if cur:
            self._remove_quietly(cur["Id"])
        self.api.container_rename(prev["Id"], name)
        self.api.container_start(prev["Id"])
        self.log(f"restarted the previous container as {name}")

    # -------------------------------------------------------------- loop
    def poll_once(self) -> bool:
        """Process at most one pending request. Returns True if one was handled."""
        try:
            req = self.take_request()
        except UpdateError as exc:
            self.status = {"state": "failed", "version": None, "from": None, "message_key": exc.key,
                           "log": [], "nonce": None, "t": 0}
            self.log(f"failed: {exc.detail}")
            return True
        if req is None:
            return False
        self.handle_request(req)
        return True

    def run_forever(self) -> None:
        self.ensure_update_dir()
        self.start_heartbeat()
        try:
            self.api.negotiate()
        except RuntimeAPIError:
            self.echo("updater: container runtime not reachable yet")
        self.recover()
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # pragma: no cover - keep the loop alive
                self.echo(f"updater: loop error {type(exc).__name__}")
            self._stop.wait(self.s.poll_s)

    # ------------------------------------------------------- diagnostics
    def check(self) -> dict:
        """One-shot diagnostics for `python -m updater --once-check` (no secrets)."""
        report: dict[str, Any] = {"sidecar_version": sidecar_version(), "ok": False,
                                  "data_dir": str(self.s.data_dir), "allow_local": self.s.allow_local}
        try:
            report["api_version"] = "%d.%d" % self.api.negotiate()
            report["runtime_reachable"] = True
        except RuntimeAPIError:
            report["runtime_reachable"] = False
            return report
        report["data_writable"] = os.access(self.s.data_dir, os.W_OK)
        try:
            t = self.find_target()
        except UpdateError as exc:
            report["target_error"] = exc.key
            return report
        repo, tag = split_ref((t.get("Config") or {}).get("Image") or "")
        report["target"] = {"name": (t.get("Name") or "").lstrip("/"), "repository": repo, "tag": tag,
                            "network_mode": self.network_mode(t), "running": bool((t.get("State") or {}).get("Running")),
                            "has_data_mount": self.data_mount(t) is not None}
        try:
            self.check_repository(repo, None)
            report["repository_allowed"] = True
        except UpdateError:
            report["repository_allowed"] = False
        url = self.health_url_for(t, self.app_port(t), self.s.health_url)
        report["health_url"] = url
        h = self.health_fn(url) if url else None
        report["health_version"] = h.get("version") if h else None
        report["ok"] = bool(report["data_writable"] and report["repository_allowed"]
                            and report["target"]["has_data_mount"] and report["health_version"])
        return report
