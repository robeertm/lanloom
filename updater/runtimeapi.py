"""Minimal client for the container runtime HTTP API over a unix socket.

Standard library only. Every call has a bounded timeout; streaming calls
(image pull) additionally have an overall deadline.
"""

from __future__ import annotations

import codecs
import http.client
import json
import socket
import struct
import time
import urllib.parse
from typing import Any, Callable

DEFAULT_SOCKET = "/var/run/docker.sock"  # identifier: container runtime socket path

# Highest API version this client was written against. The negotiated version
# is min(server version, this), but never below the server's minimum.
CLIENT_MAX_API = (1, 47)
FALLBACK_API = (1, 41)

DEFAULT_TIMEOUT = 30.0
MAX_BODY = 32 * 1024 * 1024
MAX_STREAM_LINE = 1024 * 1024


class RuntimeAPIError(Exception):
    """Error reported by the container runtime (or transport failure)."""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class NotFound(RuntimeAPIError):
    pass


class Conflict(RuntimeAPIError):
    pass


class Unreachable(RuntimeAPIError):
    def __init__(self, message: str):
        super().__init__(0, message)


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP connection over a unix domain socket."""

    def __init__(self, socket_path: str, timeout: float = DEFAULT_TIMEOUT):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except Exception:
            sock.close()
            raise
        self.sock = sock


def _parse_api(value: Any) -> tuple[int, int] | None:
    try:
        major, minor = str(value).split(".", 1)
        return int(major), int(minor)
    except (ValueError, AttributeError):
        return None


def negotiate_version(info: dict) -> tuple[int, int]:
    """Pick the API version to talk, given the server's /version answer."""
    server = _parse_api(info.get("ApiVersion")) or FALLBACK_API
    minimum = _parse_api(info.get("MinAPIVersion")) or (1, 12)
    chosen = min(server, CLIENT_MAX_API)
    if chosen < minimum:
        chosen = minimum
    return chosen


class JSONStream:
    """Incremental parser for newline-delimited (or concatenated) JSON objects.

    Tolerates partial chunks, CRLF, blank lines, garbage lines and several
    objects on one line. Lines longer than MAX_STREAM_LINE are dropped.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""
        self._json = json.JSONDecoder()

    def feed(self, data: bytes) -> list[Any]:
        self._buf += self._decoder.decode(data)
        out: list[Any] = []
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                if len(self._buf) > MAX_STREAM_LINE:
                    self._buf = ""
                break
            line, self._buf = self._buf[:nl], self._buf[nl + 1:]
            out.extend(self._parse_line(line))
        return out

    def close(self) -> list[Any]:
        self._buf += self._decoder.decode(b"", final=True)
        line, self._buf = self._buf, ""
        return self._parse_line(line)

    def _parse_line(self, line: str) -> list[Any]:
        objs = []
        pos = 0
        line = line.strip()
        while pos < len(line):
            while pos < len(line) and line[pos] in " \t\r":
                pos += 1
            if pos >= len(line):
                break
            try:
                obj, end = self._json.raw_decode(line, pos)
            except json.JSONDecodeError:
                break  # garbage: drop the rest of this line
            objs.append(obj)
            pos = end
        return objs


def demux_logs(data: bytes) -> str:
    """Decode a log stream that may use the 8-byte multiplexing header."""
    out = []
    pos = 0
    framed = len(data) >= 8 and data[0] in (0, 1, 2) and data[1:4] == b"\x00\x00\x00"
    if not framed:
        return data.decode("utf-8", errors="replace")
    while pos + 8 <= len(data):
        stream, size = data[pos], struct.unpack(">I", data[pos + 4:pos + 8])[0]
        if stream not in (0, 1, 2) or data[pos + 1:pos + 4] != b"\x00\x00\x00":
            out.append(data[pos:])
            break
        out.append(data[pos + 8:pos + 8 + size])
        pos += 8 + size
    return b"".join(out).decode("utf-8", errors="replace")


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _qimg(value: str) -> str:
    # Image references keep their slashes and tag separator in the path.
    return urllib.parse.quote(value, safe="/:@")


class RuntimeAPI:
    """Small synchronous client. One connection per request (simple, robust)."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET, timeout: float = DEFAULT_TIMEOUT):
        self.socket_path = socket_path
        self.timeout = timeout
        self.api_version: tuple[int, int] | None = None

    # ------------------------------------------------------------ transport
    def _connect(self, timeout: float | None) -> UnixHTTPConnection:
        return UnixHTTPConnection(self.socket_path, timeout=timeout or self.timeout)

    def _path(self, path: str, query: dict | None, versioned: bool) -> str:
        if versioned:
            if self.api_version is None:
                self.negotiate()
            major, minor = self.api_version  # type: ignore[misc]
            path = f"/v{major}.{minor}{path}"
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                path += "?" + urllib.parse.urlencode(clean)
        return path

    def _send(self, method: str, path: str, query: dict | None, body: Any,
              timeout: float | None, versioned: bool = True):
        conn = self._connect(timeout)
        headers = {"Host": "localhost"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, self._path(path, query, versioned), body=payload, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise Unreachable(f"{type(exc).__name__}: {exc}") from None
        return conn, resp

    @staticmethod
    def _error(status: int, data: bytes) -> RuntimeAPIError:
        message = data.decode("utf-8", errors="replace").strip()
        try:
            message = str(json.loads(message).get("message", message))
        except (ValueError, AttributeError):
            pass
        message = message[:500]
        if status == 404:
            return NotFound(status, message)
        if status == 409:
            return Conflict(status, message)
        return RuntimeAPIError(status, message)

    def request(self, method: str, path: str, query: dict | None = None, body: Any = None,
                timeout: float | None = None, versioned: bool = True, raw: bool = False) -> Any:
        conn, resp = self._send(method, path, query, body, timeout, versioned)
        try:
            data = resp.read(MAX_BODY + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise Unreachable(f"{type(exc).__name__}: {exc}") from None
        finally:
            conn.close()
        if len(data) > MAX_BODY:
            raise RuntimeAPIError(resp.status, "response too large")
        if resp.status >= 400:
            raise self._error(resp.status, data)
        if raw:
            return data
        if not data.strip():
            return None
        try:
            return json.loads(data)
        except ValueError:
            return None

    # --------------------------------------------------------------- system
    def negotiate(self) -> tuple[int, int]:
        info = self.request("GET", "/version", versioned=False, timeout=10)  # identifier: runtime API path
        self.api_version = negotiate_version(info or {})
        return self.api_version

    def version(self) -> dict:
        return self.request("GET", "/version", versioned=False, timeout=10) or {}  # identifier: runtime API path

    def api_at_least(self, major: int, minor: int) -> bool:
        if self.api_version is None:
            self.negotiate()
        return self.api_version >= (major, minor)  # type: ignore[operator]

    # ----------------------------------------------------------- containers
    def containers(self, labels: list[str] | None = None, all: bool = True) -> list[dict]:
        filters = {"label": labels} if labels else {}
        query = {"all": "1" if all else "0", "filters": json.dumps(filters) if filters else None}
        return self.request("GET", "/containers/json", query) or []  # identifier: runtime API path

    def container_inspect(self, ref: str) -> dict:
        return self.request("GET", f"/containers/{_q(ref)}/json")  # identifier: runtime API path

    def container_create(self, name: str | None, body: dict) -> str:
        res = self.request("POST", "/containers/create", {"name": name}, body)  # identifier: runtime API path
        return res["Id"]

    def container_start(self, ref: str) -> None:
        self.request("POST", f"/containers/{_q(ref)}/start")  # identifier: runtime API path

    def container_stop(self, ref: str, t: int = 20) -> None:
        self.request("POST", f"/containers/{_q(ref)}/stop", {"t": str(t)},  # identifier: runtime API path
                     timeout=t + 15)

    def container_remove(self, ref: str, force: bool = True) -> None:
        self.request("DELETE", f"/containers/{_q(ref)}",  # identifier: runtime API path
                     {"force": "1" if force else "0", "v": "1"}, timeout=60)

    def container_rename(self, ref: str, name: str) -> None:
        self.request("POST", f"/containers/{_q(ref)}/rename", {"name": name})  # identifier: runtime API path

    def container_logs(self, ref: str, tail: int = 20) -> list[str]:
        data = self.request("GET", f"/containers/{_q(ref)}/logs",  # identifier: runtime API path
                            {"stdout": "1", "stderr": "1", "tail": str(tail)}, raw=True, timeout=15)
        text = demux_logs(data or b"")
        return [ln for ln in text.splitlines() if ln.strip()][-tail:]

    def network_connect(self, network: str, container: str, endpoint: dict) -> None:
        self.request("POST", f"/networks/{_q(network)}/connect",  # identifier: runtime API path
                     body={"Container": container, "EndpointConfig": endpoint})

    # --------------------------------------------------------------- images
    def image_inspect(self, ref: str) -> dict | None:
        try:
            return self.request("GET", f"/images/{_qimg(ref)}/json")  # identifier: runtime API path
        except NotFound:
            return None

    def image_tag(self, ref: str, repo: str, tag: str) -> None:
        self.request("POST", f"/images/{_qimg(ref)}/tag", {"repo": repo, "tag": tag})  # identifier: runtime API path

    def image_pull(self, repo: str, tag: str, progress: Callable[[dict], None] | None = None,
                   deadline_s: float = 900, read_timeout: float = 120) -> dict:
        """Pull anonymously; returns the last status object. Raises on error."""
        deadline = time.monotonic() + deadline_s
        conn, resp = self._send("POST", "/images/create",  # identifier: runtime API path
                                {"fromImage": repo, "tag": tag}, None, read_timeout)
        last: dict = {}
        try:
            if resp.status >= 400:
                raise self._error(resp.status, resp.read(65536))
            parser = JSONStream()
            while True:
                if time.monotonic() > deadline:
                    raise RuntimeAPIError(0, "pull deadline exceeded")
                try:
                    chunk = resp.read1(65536)
                except (OSError, http.client.HTTPException) as exc:
                    raise Unreachable(f"{type(exc).__name__}: {exc}") from None
                objs = parser.feed(chunk) if chunk else parser.close()
                for obj in objs:
                    if not isinstance(obj, dict):
                        continue
                    if obj.get("error") or obj.get("errorDetail"):
                        detail = obj.get("errorDetail") or {}
                        msg = obj.get("error") or (detail.get("message") if isinstance(detail, dict) else "")
                        raise RuntimeAPIError(500, str(msg)[:300] or "pull failed")
                    last = obj
                    if progress:
                        progress(obj)
                if not chunk:
                    break
        finally:
            conn.close()
        return last
