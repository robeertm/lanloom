"""Local AI service management for the update sidecar.

The app asks through a small trigger file on the shared data volume (protocol: ``lanloom/ai/runtime.py``):

    <data>/update/ai-request.json   {"action": "install"|"remove", "nonce": <32 hex>, "requested": <unix time>}
    <data>/update/ai-status.json    {"state", "message_key", "url", "nonce", "t", "log": [...]}

Only ``action``, ``nonce`` and ``requested`` are ever read from the request. Image, container name, environment,
mounts and labels all come from ``lanloom.ai.runtime``; nothing from the file reaches a command or a container
setting. Models live in a named volume that this module never deletes.
"""

from __future__ import annotations

import os
import re
import socket
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from lanloom.ai import runtime as R

from .engine import (LOG_LINE_MAX, LOG_LINES, ROLE_LABEL, REQUEST_MAX_BYTES,
                     Settings, UpdateError, Updater, atomic_write_json, read_json_file, _chown, _owner_of)
from .runtimeapi import NotFound, RuntimeAPI, RuntimeAPIError, Unreachable

AI_NONCE_RE = re.compile(r"\A[0-9a-f]{32}\Z")
PARENT_LABEL = "org.lanloom.ai.parent"
PULL_DEADLINE_S = 1800
READY_TIMEOUT_S = 90.0
STALE_REQUEST_S = 3600.0

MESSAGE_KEYS = (
    "ai.local.sidecar_running",
    "ai.local.removed",
    "ai.err.invalid_request",
    "ai.err.port_busy",
    "ai.err.pull_failed",
    "ai.err.create_failed",
    "ai.err.start_failed",
    "ai.err.no_runtime",
    "ai.err.no_target",
    "ai.err.not_ours",
    "ai.err.internal",
)


class AIError(Exception):
    def __init__(self, key: str, detail: str = ""):
        super().__init__(detail or key)
        self.key = key
        self.detail = detail


def _clean(text: Any) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    return " ".join(text.split())[:LOG_LINE_MAX]


def default_http_get(url: str, timeout: float = 3.0) -> int | None:
    """Status code of a GET, or None when nothing answers."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # fixed loopback/service URL, never from a request
            return resp.status
    except Exception:
        return None


def default_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


class LocalAI:
    def __init__(self, api: RuntimeAPI, settings: Settings, clock: Callable[[], float] = time.time,
                 echo: Callable[[str], None] | None = None,
                 http_get: Callable[[str], int | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 port_open: Callable[[int], bool] = default_port_open,
                 find_target: Callable[[], dict] | None = None,
                 owner: Callable[[], tuple[int, int] | None] | None = None):
        self.api = api
        self.s = settings
        self.clock = clock
        self.sleep = sleep
        self.echo = echo if echo is not None else (lambda line: print(line, flush=True))
        self.http_get = http_get or default_http_get
        self.port_open = port_open
        self._find_target = find_target or (lambda: Updater.find_target(self))  # type: ignore[arg-type]
        self._owner = owner
        self.status: dict = {"state": "idle", "message_key": None, "url": None, "nonce": None, "t": 0, "log": []}

    # ------------------------------------------------------------- paths
    @property
    def update_dir(self) -> Path:
        return self.s.data_dir / "update"

    @property
    def request_path(self) -> Path:
        return self.update_dir / R.REQUEST_FILE

    @property
    def status_path(self) -> Path:
        return self.update_dir / R.STATUS_FILE

    def data_owner(self) -> tuple[int, int] | None:
        if self._owner is not None:
            return self._owner()
        db = self.s.data_dir / "lanloom.db"
        return _owner_of(db if db.exists() else self.s.data_dir)

    # ------------------------------------------------------------ status
    def _write(self) -> None:
        self.status["t"] = round(self.clock(), 3)
        try:
            d = self.update_dir
            if d.is_symlink():
                raise OSError("update dir is a symlink")
            if not d.exists():
                d.mkdir(mode=0o755, parents=True)
                _chown(d, self.data_owner())
            atomic_write_json(self.status_path, self.status, self.data_owner())
        except OSError as exc:
            self.echo(f"local-ai: cannot write status: {type(exc).__name__}")

    def log(self, line: str) -> None:
        line = _clean(line)
        self.status["log"] = (self.status.get("log") or [])[-(LOG_LINES - 1):] + [line]
        self.echo(f"local-ai: {line}")
        self._write()

    def set_state(self, state: str, message_key: str | None = None, url: str | None = None,
                  log: str | None = None) -> None:
        assert state in R.STATES, state
        self.status["state"] = state
        if message_key is not None:
            self.status["message_key"] = message_key
        if url is not None:
            self.status["url"] = url
        if log:
            self.log(log)
        else:
            self._write()

    def _begin(self, nonce: str | None) -> None:
        self.status = {"state": "idle", "message_key": None, "url": None, "nonce": nonce, "t": 0, "log": []}

    # ----------------------------------------------------------- request
    def take_request(self) -> dict | None:
        if not self.request_path.exists() and not self.request_path.is_symlink():
            return None
        try:
            req = read_json_file(self.request_path, REQUEST_MAX_BYTES)
        finally:
            try:
                os.unlink(self.request_path)
            except FileNotFoundError:
                pass
        if req is None:
            return None
        if not isinstance(req, dict):
            raise UpdateError("ai.err.invalid_request", "request is not an object")
        return req

    def validate(self, req: dict) -> tuple[str, str]:
        nonce = req.get("nonce")
        if not isinstance(nonce, str) or not AI_NONCE_RE.match(nonce):
            raise AIError("ai.err.invalid_request", "missing or malformed nonce")
        action = req.get("action")
        if not isinstance(action, str) or action not in R.ACTIONS:
            raise AIError("ai.err.invalid_request", "unknown action")
        requested = req.get("requested")
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise AIError("ai.err.invalid_request", "missing request time")
        now = self.clock()
        if requested < now - STALE_REQUEST_S or requested > now + 300:
            raise AIError("ai.err.invalid_request", "request is too old or from the future")
        return action, nonce

    # -------------------------------------------------------------- loop
    def poll_once(self) -> bool:
        """Process at most one pending AI request. Returns True if one was handled."""
        try:
            req = self.take_request()
        except UpdateError as exc:
            self._begin(None)
            self.set_state("failed", "ai.err.invalid_request", log=f"failed: {exc.detail}")
            return True
        if req is None:
            return False
        try:
            action, nonce = self.validate(req)
        except AIError as exc:
            nonce = req.get("nonce")
            self._begin(nonce if isinstance(nonce, str) and AI_NONCE_RE.match(nonce) else None)
            self.set_state("failed", exc.key, log=f"failed: {exc.detail}")
            return True
        self._begin(nonce)
        try:
            if action == "install":
                self.install()
            else:
                self.remove()
        except AIError as exc:
            self.set_state("failed", exc.key, log=f"failed: {exc.detail or exc.key}")
        except Unreachable:
            self.set_state("failed", "ai.err.no_runtime", log="failed: container runtime not reachable")
        except Exception as exc:  # keep the sidecar loop alive
            self.set_state("failed", "ai.err.internal", log=f"failed: {type(exc).__name__}")
        return True

    # ----------------------------------------------------------- helpers
    def _target(self) -> dict:
        try:
            return self._find_target()
        except UpdateError as exc:
            if exc.key == "update.err.runtime_unreachable":
                raise AIError("ai.err.no_runtime", "container runtime not reachable") from None
            raise AIError("ai.err.no_target", "Lanloom container not found") from None
        except Unreachable:
            raise AIError("ai.err.no_runtime", "container runtime not reachable") from None

    def _existing(self) -> dict | None:
        try:
            return self.api.container_inspect(R.CONTAINER)
        except NotFound:
            return None

    @staticmethod
    def _is_ours(container: dict) -> bool:
        labels = (container.get("Config") or {}).get("Labels") or {}
        return labels.get(ROLE_LABEL) == R.ROLE

    def _remove_quietly(self, ref: str) -> None:
        try:
            self.api.container_remove(ref, force=True)
        except RuntimeAPIError:
            pass

    # ----------------------------------------------------------- install
    def install(self) -> None:
        target = self._target()
        app_name = (target.get("Name") or "").lstrip("/")
        mode = Updater.network_mode(target)
        host = mode == "host"
        url = R.service_url(host)

        existing = self._existing()
        if existing is not None:
            if not self._is_ours(existing):
                raise AIError("ai.err.not_ours", "a foreign container uses the service name")
            if not (existing.get("State") or {}).get("Running"):
                self.set_state("starting", log="starting the existing local AI service")
                try:
                    self.api.container_start(R.CONTAINER)
                except RuntimeAPIError as exc:
                    raise AIError("ai.err.start_failed", f"start failed: {exc.message}") from None
            self._wait_ready(url, host)
            self.set_state("running", "ai.local.sidecar_running", url=url, log="local AI service is running")
            return

        if host and self.port_open(R.PORT):
            raise AIError("ai.err.port_busy", f"something already listens on 127.0.0.1:{R.PORT}")

        self._pull()

        self.set_state("creating", log="creating the local AI service")
        net = "bridge" if mode in ("default", "") else mode
        body: dict = {
            "Image": R.image_ref(),
            "Env": R.container_env(host),
            "Labels": {ROLE_LABEL: R.ROLE, PARENT_LABEL: app_name},
            "HostConfig": {
                "NetworkMode": "host" if host else net,
                "RestartPolicy": {"Name": "unless-stopped"},
                "Mounts": [{"Type": "volume", "Source": R.VOLUME, "Target": R.MODELS_DIR}],
                "SecurityOpt": ["no-new-privileges:true"],
            },
        }
        if not host:
            body["NetworkingConfig"] = {"EndpointsConfig": {net: {"Aliases": [R.CONTAINER]}}}
        try:
            self.api.container_create(R.CONTAINER, body)
        except RuntimeAPIError as exc:
            raise AIError("ai.err.create_failed", f"create failed: {exc.message}") from None

        self.set_state("starting", log="starting the local AI service")
        try:
            self.api.container_start(R.CONTAINER)
            self._wait_ready(url, host)
        except RuntimeAPIError as exc:
            self._remove_quietly(R.CONTAINER)
            raise AIError("ai.err.start_failed", f"start failed: {exc.message}") from None
        except AIError:
            self._remove_quietly(R.CONTAINER)
            raise
        self.set_state("running", "ai.local.sidecar_running", url=url, log="local AI service is running")

    def _pull(self) -> None:
        ref = R.image_ref().split("@", 1)[0]
        slash, colon = ref.rfind("/"), ref.rfind(":")
        repo, tag = (ref[:colon], ref[colon + 1:]) if colon > slash else (ref, "latest")
        self.set_state("pulling", log=f"pulling {repo}:{tag}")
        seen = {"t": self.clock()}

        def progress(obj: dict) -> None:
            now = self.clock()
            if now - seen["t"] < 5:
                return
            seen["t"] = now
            detail = obj.get("progressDetail") or {}
            cur, tot = detail.get("current"), detail.get("total")
            if isinstance(cur, (int, float)) and isinstance(tot, (int, float)) and tot > 0:
                self.log(f"pulling {int(100 * cur / tot)} %")
            elif obj.get("status"):
                self.log(f"pulling: {obj.get('status')}")

        try:
            self.api.image_pull(repo, tag, progress, deadline_s=PULL_DEADLINE_S)
        except RuntimeAPIError as exc:
            raise AIError("ai.err.pull_failed", f"pull failed: {exc.message}") from None

    def _wait_ready(self, url: str, host: bool) -> None:
        if not host:
            return  # the sidecar may not reach the service by name; the app checks itself
        deadline = self.clock() + READY_TIMEOUT_S
        while True:
            if self.http_get(url + "/api/version") == 200:
                return
            if self.clock() >= deadline:
                raise AIError("ai.err.start_failed", "the service did not answer in time")
            self.sleep(1.0)

    # ------------------------------------------------------------ remove
    def remove(self) -> None:
        existing = self._existing()
        if existing is None:
            self.set_state("removed", "ai.local.removed", log="no local AI service present")
            return
        if not self._is_ours(existing):
            raise AIError("ai.err.not_ours", "the container is not a Lanloom local AI service")
        self.set_state("removing", log="removing the local AI service (models stay in their volume)")
        try:
            self.api.container_stop(R.CONTAINER)
        except NotFound:
            pass
        except RuntimeAPIError as exc:
            self.log(f"stop: {exc.message}")
        try:
            self.api.container_remove(R.CONTAINER, force=True)
        except NotFound:
            pass
        except RuntimeAPIError as exc:
            raise AIError("ai.err.internal", f"remove failed: {exc.message}") from None
        self.set_state("removed", "ai.local.removed", log="local AI service removed")

    # ------------------------------------------------------- diagnostics
    def check(self) -> dict:
        try:
            c = self._existing()
        except RuntimeAPIError:
            return {"container": False, "running": False}
        return {"container": c is not None, "running": bool(c and (c.get("State") or {}).get("Running"))}
