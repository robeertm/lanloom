"""Stand-ins for the AI services: a request factory (the ``opener`` of the adapter) and a small fake core."""
import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from types import SimpleNamespace

from lanloom.ai.providers import AIError, _http_error

KEY = "sk-test-0123456789abcdefSECRET"


def sse(*objs, done=True) -> list:
    out = []
    for o in objs:
        out += ["data: " + json.dumps(o), ""]
    if done:
        out += ["data: [DONE]", ""]
    return out


def ndjson(*objs) -> list:
    return [json.dumps(o) for o in objs]


class FakeStream:
    def __init__(self, lines):
        self._lines = [lines] if isinstance(lines, str) else list(lines)
        self.closed = False

    def lines(self):
        yield from self._lines

    def read_all(self) -> bytes:
        return "\n".join(self._lines).encode("utf-8")

    def close(self):
        self.closed = True


@dataclass
class Call:
    url: str
    method: str
    headers: dict
    body: object
    limits: object
    path: str = field(init=False)

    def __post_init__(self):
        parts = urllib.parse.urlsplit(self.url)
        self.path = parts.path + ("?" + parts.query if parts.query else "")


class Fake:
    """``Fake().on("/api/tags", json_or_lines)`` then pass the instance as ``opener``.

    A response is a list of lines, a dict (sent as one JSON document), an ``AIError`` (raised, like the real
    request factory does), a number (HTTP status, mapped like the real one) or a callable ``f(call)``."""

    def __init__(self):
        self.routes = []
        self.calls = []
        self.lock = threading.Lock()

    def on(self, suffix: str, response, method: str = None):
        self.routes.append((suffix, method, response))
        return self

    def paths(self) -> list:
        return [c.path for c in self.calls]

    def __call__(self, url, method, headers, body, limits):
        try:
            parsed = json.loads(body.decode("utf-8")) if body else None
        except ValueError:
            parsed = None
        call = Call(url, method, dict(headers), parsed, limits)
        with self.lock:
            self.calls.append(call)
        bare = call.path.split("?")[0]
        for suffix, m, response in self.routes:
            if m is not None and m != method:
                continue
            if bare.endswith(suffix) or ("?" in suffix and suffix in call.path):
                return self._respond(call, response)
        raise AIError("ai.err.unreachable", "no route in the test")

    def _respond(self, call, response):
        if callable(response) and not isinstance(response, (list, dict)):
            response = response(call)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, int):
            raise _http_error(response, b"")
        if isinstance(response, dict):
            return FakeStream(json.dumps(response))
        return FakeStream(response)


class FakeCore:
    """The few things the AI manager asks of the application core."""

    def __init__(self, devices=None, integrations=None, live=None):
        self._devices = devices if devices is not None else []
        self._integrations = integrations or []
        self._live = live or []
        self.store = SimpleNamespace(device=self._device)
        self.reports = SimpleNamespace(get=lambda day: None, tz=lambda: None)

    def _device(self, did):
        return next((d for d in self._devices if d["id"] == did), None)

    def devices(self):
        return list(self._devices)

    def integrations(self):
        return list(self._integrations)

    def live_configs(self):
        return list(self._live)

    def device_view(self, d):
        return d


def device(did="dev1", ip="192.168.1.20", **extra):
    d = {"id": did, "title": "Attic Printer", "kind": "printer", "ip": ip, "mac": "aa:bb:cc:dd:ee:01",
         "hostnames": ["attic-printer.lan"], "status": "up", "certainty": "high", "confidence": 0.9,
         "first_seen": 1_700_000_000, "last_seen": 1_700_100_000, "services": ["ipp"], "ports": [{"port": 631, "service": "ipp"}]}
    d.update(extra)
    return d
