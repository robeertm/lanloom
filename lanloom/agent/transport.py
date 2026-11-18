"""The agent's only way out: pairing with one Lanloom and sending reports to it, with a buffer for outages.

* **Pairing**: the one-time code from Lanloom's settings is exchanged once for a token of this host
  (``POST /api/agent/pair``). The token is kept in the agent's state directory (file mode 0600); Lanloom stores only
  its hash and can revoke it at any time.
* **Reports** go to ``POST /api/agent/report`` of exactly that Lanloom, with the token. The agent opens no port and
  takes no commands: the answer can only say "fine", "revoked" or which version Lanloom runs.
* **Buffer**: when Lanloom cannot be reached, reports wait in memory (and in the state directory, so a restart keeps
  them) for up to ``BUFFER_MAX`` reports; older ones are thinned out first. They are sent oldest first once Lanloom
  answers again.
* **Updates**: the agent never loads code. When Lanloom runs another version, the agent says so in its log; the
  update is a pull of the signed Lanloom image with exactly Lanloom's version (see docs/HOST-AGENT.md).

Optional ``LANLOOM_AGENT_PIN`` (sha256 of Lanloom's certificate) pins an HTTPS address with its own certificate.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import os
import ssl
import time
from collections import deque
from urllib.parse import urlsplit

from . import scrub

log = logging.getLogger("lanloom.agent")

STATE_DIR = "/var/lib/lanloom-agent"
BUFFER_MAX = 720                 # two hours of 10-second reports
BUFFER_KEEP_FULL = 30            # the newest reports in the buffer keep all parts; older ones keep bytes and system only
BATCH = 30
TIMEOUT = 10.0
MAX_BODY = 4 << 20


class PairError(Exception):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def normalise_server(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment:
        return ""
    return url


def code_hash(code: str) -> str:
    return hashlib.sha256(("lanloom-agent-code:" + "".join(ch for ch in (code or "").upper() if ch.isalnum())).encode()).hexdigest()


class Link:
    def __init__(self, server: str, state_dir: str = STATE_DIR, version: str = "", pin: str = ""):
        self.server = normalise_server(server)
        if not self.server:
            raise PairError("agent.err.server_url")
        self.state_dir = state_dir
        self.version = version
        self.pin = (pin or "").lower().replace(":", "").removeprefix("sha256")
        self.cred: dict = self._load()
        self.buffer: deque = deque(self._load_buffer(), maxlen=BUFFER_MAX)
        self.server_version = ""
        self.revoked = False
        self.last_ok = 0.0
        self._said_version = 0.0
        self._buffer_saved = 0.0

    # ------------------------------------------------------------------ state files
    def _path(self, name: str) -> str:
        return os.path.join(self.state_dir, name)

    def _load(self) -> dict:
        try:
            with open(self._path("token.json"), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) and data.get("token") else {}
        except (OSError, ValueError):
            return {}

    def _write(self, name: str, data) -> None:
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        tmp = self._path(name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, self._path(name))

    def _load_buffer(self) -> list:
        try:
            with open(self._path("buffer.json"), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return [r for r in data if isinstance(r, dict)][-BUFFER_MAX:] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def save_buffer(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._buffer_saved < 60:
            return
        self._buffer_saved = now
        try:
            if self.buffer:
                self._write("buffer.json", list(self.buffer))
            elif os.path.exists(self._path("buffer.json")):
                os.remove(self._path("buffer.json"))
        except OSError:
            log.warning("agent: could not keep the buffer in %s", self.state_dir)

    @property
    def paired(self) -> bool:
        return bool(self.cred.get("token")) and self.cred.get("server") == self.server

    # ------------------------------------------------------------------ HTTP
    def _post(self, path: str, body: dict, token: str = "") -> tuple:
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BODY:
            raise PairError("agent.err.too_large")
        p = urlsplit(self.server)
        if p.scheme == "https":
            ctx = ssl.create_default_context()
            if self.pin:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE           # the pin below replaces the certificate chain check
            conn = http.client.HTTPSConnection(p.hostname, p.port or 443, timeout=TIMEOUT, context=ctx)
        else:
            conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=TIMEOUT)
        headers = {"Content-Type": "application/json", "X-Lanloom": "1", "User-Agent": "lanloom-agent/%s" % self.version}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            conn.connect()
            if p.scheme == "https" and self.pin:
                der = conn.sock.getpeercert(binary_form=True) or b""
                if not hmac.compare_digest(hashlib.sha256(der).hexdigest(), self.pin):
                    raise PairError("agent.err.pin")
            conn.request("POST", (p.path or "") + path, body=raw, headers=headers)
            resp = conn.getresponse()
            data = resp.read(1 << 20)
            try:
                answer = json.loads(data.decode("utf-8", "replace")) if data else {}
            except ValueError:
                answer = {}
            return resp.status, answer if isinstance(answer, dict) else {}
        finally:
            conn.close()

    # ------------------------------------------------------------------ pairing
    def pair(self, code: str, facts: dict, proto: int) -> dict:
        try:
            status, answer = self._post("/api/agent/pair", {"code": code, "proto": proto, "version": self.version, "host": facts})
        except (OSError, http.client.HTTPException) as exc:
            raise PairError("agent.err.unreachable") from exc
        if status != 200 or not answer.get("token"):
            raise PairError(str(answer.get("error") or "agent.err.pair_refused")[:64])
        self.cred = {"server": self.server, "agent_id": str(answer.get("agent_id") or "")[:40], "token": str(answer["token"]),
                     "code": code_hash(code), "paired": round(time.time())}
        self._write("token.json", self.cred)
        self.revoked = False
        self.server_version = str(answer.get("server_version") or "")[:32]
        return answer

    def forget(self) -> None:
        """The token was revoked: drop it and the reports that wait for it."""
        self.cred = {}
        self.buffer.clear()
        for name in ("token.json", "buffer.json"):
            try:
                os.remove(self._path(name))
            except OSError:
                pass

    def used_code(self, code: str) -> bool:
        return bool(code) and hmac.compare_digest(self.cred.get("code", ""), code_hash(code))

    # ------------------------------------------------------------------ reports
    def queue(self, report: dict) -> None:
        bad = scrub.check(report)
        if bad:                                         # never send a forbidden field, even if a collector had a bug
            log.error("agent: dropped forbidden fields %s", ", ".join(bad[:5]))
            report = scrub.strip(report)
        self.buffer.append(report)
        if len(self.buffer) > BUFFER_KEEP_FULL:         # thin out the older reports: they keep bytes and system values
            old = self.buffer[-BUFFER_KEEP_FULL - 1]
            for k in ("listen", "units", "overlay", "host"):
                old.pop(k, None)
            if isinstance(old.get("containers"), dict):
                items = old["containers"].get("items") or []
                old["containers"] = {"available": old["containers"].get("available"), "bytes_only": True,
                                     "items": [{k: c[k] for k in ("id", "name", "state", "tx", "rx") if k in c} for c in items]}

    def flush(self, proto: int) -> str:
        """Send what waits, oldest first. -> "ok" | "offline" | "revoked" | "refused" | "empty"."""
        if not self.buffer:
            return "empty"
        if not self.paired:
            return "refused"
        while self.buffer:
            batch = [self.buffer[i] for i in range(min(BATCH, len(self.buffer)))]
            try:
                status, answer = self._post("/api/agent/report", {"proto": proto, "version": self.version, "reports": batch},
                                            self.cred["token"])
            except (OSError, http.client.HTTPException, PairError) as exc:
                log.info("agent: Lanloom not reachable (%s), %d reports wait", type(exc).__name__, len(self.buffer))
                self.save_buffer()
                return "offline"
            if status == 401:
                self.revoked = True
                log.warning("agent: Lanloom refused this agent's token (revoked?). Pair again with a new code.")
                return "revoked"
            if status != 200:
                log.warning("agent: Lanloom answered %s %s", status, answer.get("error", ""))
                if status in (400, 409, 413, 422):
                    for _ in batch:                     # a report Lanloom cannot take never blocks the ones after it
                        self.buffer.popleft()
                    continue
                self.save_buffer()
                return "refused"
            for _ in batch:
                self.buffer.popleft()
            self.last_ok = time.time()
            self._note_version(str(answer.get("server_version") or "")[:32])
        self.save_buffer(force=True)
        return "ok"

    def _note_version(self, server_version: str) -> None:
        self.server_version = server_version
        if server_version and server_version != self.version and time.time() - self._said_version > 3600:
            self._said_version = time.time()
            log.warning("agent: Lanloom runs %s, this agent %s. Update the agent to the same signed image: "
                        "ghcr.io/robeertm/lanloom:%s (the agent never loads code by itself).", server_version, self.version,
                        server_version)
