"""AI providers behind one interface: model list, chat, connection test.

Local: the native API of the local model server and any OpenAI-compatible local server (LM Studio, llama.cpp server,
vLLM). Public: OpenAI, Anthropic, Google Gemini, Mistral, OpenRouter and any OpenAI-compatible address.
Product names are used only to identify compatible services. No affiliation or endorsement: Lanloom is not
affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.

Every call streams the answer and is bounded by three limits (see ``Limits``):

1. ``connect`` - the connection is accepted (TCP + TLS) and the request is on its way. Fails fast for a host that
   is off, so "down" is never mistaken for "busy".
2. ``first`` - from the sent request to the first byte of the answer (the server's acknowledgement). A local model
   on a small computer may need a while to load and read the prompt; that is load, not a hang.
3. ``done`` - from the first byte to the end of the answer, with an ``idle`` gap limit between two pieces.

Secrets travel as ``Secret`` (``lanloom/integrations/credentials.py``) and only the request builder reveals them; no
error text ever contains a header, a key or a full answer body.
"""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Optional

from ..integrations.credentials import Secret

MAX_BODY = 4 * 1024 * 1024
MAX_MODELS = 600
MAX_ANSWER_CHARS = 8000


class AIError(Exception):
    """``key`` is an i18n key (``ai.err.*``); ``detail`` is short, never contains secrets or answer bodies."""

    def __init__(self, key: str, detail: str = "", status: int = 0):
        super().__init__(key)
        self.key = key
        self.detail = detail[:200]
        self.status = status


@dataclass(frozen=True)
class Limits:
    connect: float
    first: float
    done: float
    idle: float


LOCAL_LIMITS = Limits(connect=5.0, first=150.0, done=240.0, idle=60.0)
PUBLIC_LIMITS = Limits(connect=10.0, first=45.0, done=90.0, idle=30.0)
TEST_LIMITS_LOCAL = Limits(connect=5.0, first=150.0, done=120.0, idle=60.0)
LIST_TIMEOUT = 8.0


@dataclass(frozen=True)
class Provider:
    kind: str
    scope: str                 # "local" | "public"
    label: str                 # shown in the selection list
    dialect: str               # "native" | "openai" | "anthropic" | "gemini" | "demo"
    base_url: str = ""         # fixed address of a public service ("" = the user enters one)
    key: str = "none"          # "required" | "optional" | "none"
    prefer: tuple = ()         # regexes, first hit in the model list is the recommended model
    hint: str = ""             # placeholder for the address field
    demo_only: bool = False

    def public(self) -> dict:
        return {"kind": self.kind, "scope": self.scope, "label": self.label, "fixed_url": bool(self.base_url),
                "key": self.key, "hint": self.hint}


NO_AFFILIATION = ("Product names are used only to identify compatible services. No affiliation or endorsement: "
                  "Lanloom is not affiliated with, endorsed or sponsored by their owners.")

PROVIDERS = {p.kind: p for p in (
    Provider("ollama", "local", "Ollama (local)", "native", key="none", hint="http://127.0.0.1:11434",
             prefer=(r"^llama3\.1:8b", r"^qwen2\.5:7b", r"^llama3\.2:3b", r"^qwen2\.5:3b", r"^gemma\d?:\d+b")),
    Provider("openai_local", "local", "OpenAI-compatible local server (LM Studio, llama.cpp, vLLM)", "openai",
             key="optional", hint="http://127.0.0.1:1234/v1"),
    Provider("openai", "public", "OpenAI", "openai", "https://api.openai.com/v1", "required",
             prefer=(r"^gpt-5(\.\d+)?-mini$", r"^gpt-4\.1-mini$", r"^gpt-4o-mini$", r"mini$")),
    Provider("anthropic", "public", "Anthropic", "anthropic", "https://api.anthropic.com", "required",
             prefer=(r"haiku", r"sonnet")),
    Provider("gemini", "public", "Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta", "required",
             prefer=(r"flash-lite$", r"flash$", r"flash")),
    Provider("mistral", "public", "Mistral", "openai", "https://api.mistral.ai/v1", "required",
             prefer=(r"^mistral-small-latest$", r"small")),
    Provider("openrouter", "public", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "required",
             prefer=(r"^openrouter/auto$", r"mini$", r"haiku", r"flash")),
    Provider("openai_compat", "public", "Other OpenAI-compatible service", "openai", key="optional",
             hint="https://example.org/v1"),
    # the sample setup (demo mode) shows no product names and never leaves the process
    Provider("demo", "local", "Sample local AI server", "demo", key="none", demo_only=True),
    Provider("demo_cloud", "public", "Sample cloud AI service", "demo", key="optional", demo_only=True),
)}

UNUSABLE_MODEL = re.compile(r"embed|bge-|clip|rerank|nomic-|whisper|tts|dall|image|moderation|audio|transcribe|realtime|search",
                            re.I)


def get(kind: str) -> Provider:
    p = PROVIDERS.get(kind)
    if p is None:
        raise AIError("ai.err.provider")
    return p


def public_list(demo: bool = False) -> list:
    """The selection list: the real services, or in demo mode only the two sample services."""
    return [p.public() for p in PROVIDERS.values() if p.demo_only == demo]


# ---------------------------------------------------------------------- addresses
def normalise_url(kind: str, url: str) -> str:
    """The base address a provider's requests are built on. Users paste the bare address, with or without ``/v1``,
    or even the full chat path; the adapter makes one form out of all of them."""
    p = get(kind)
    if p.base_url:
        return p.base_url
    raw = (url or "").strip()
    if not raw:
        raise AIError("ai.err.url")
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
        host, port = parts.hostname, parts.port      # both can raise ValueError (bad IPv6 form, bad port)
    except ValueError:
        raise AIError("ai.err.url") from None
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise AIError("ai.err.url")
    if parts.query or parts.fragment:
        raise AIError("ai.err.url")
    path = parts.path.rstrip("/")
    for tail in ("/chat/completions", "/completions", "/models", "/api/chat", "/api/tags", "/api/generate", "/api"):
        if path.endswith(tail):
            path = path[: -len(tail)]
            break
    path = path.rstrip("/")
    if p.dialect == "native":
        if path.endswith("/v1"):
            path = path[:-3]
    elif p.dialect == "openai":
        if not re.search(r"/v\d+(beta)?$", path) and not path.endswith("/api/v1"):
            path += "/v1"
    netloc = host if ":" not in host else "[%s]" % host
    if port:
        netloc += ":%d" % port
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", ""))


def host_is_local(url: str, resolve: Callable = socket.getaddrinfo) -> bool:
    """True when every address of the URL's host is private, loopback or link-local. A "local" provider whose
    address leads to the internet is treated as public (it then gets placeholders like any public service)."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if not host:
        return False
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = resolve(host, None)
        except (OSError, UnicodeError):
            return False
        addrs = []
        for info in infos:
            try:
                addrs.append(ipaddress.ip_address(info[4][0].split("%")[0]))
            except (ValueError, IndexError):
                return False
    if not addrs:
        return False
    for a in addrs:
        if getattr(a, "ipv4_mapped", None):
            a = a.ipv4_mapped
        if not (a.is_private or a.is_loopback or a.is_link_local) or a.is_multicast:
            return False
    return True


# ---------------------------------------------------------------------- HTTP with three limits
class _Stream:
    """One HTTP request whose answer is read line by line under the connect / first / done limits."""

    def __init__(self, url: str, method: str, headers: dict, body: Optional[bytes], limits: Limits,
                 clock: Callable = time.monotonic):
        self.limits = limits
        self.clock = clock
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        try:
            if parts.scheme == "https":
                ctx = ssl.create_default_context()
                self.conn = http.client.HTTPSConnection(host, port or 443, timeout=limits.connect, context=ctx)
            else:
                self.conn = http.client.HTTPConnection(host, port or 80, timeout=limits.connect)
            self.conn.connect()
            self.sock = self.conn.sock      # http.client drops conn.sock once an answer is "Connection: close"
        except ssl.SSLError:
            raise AIError("ai.err.tls") from None
        except socket.timeout:
            raise AIError("ai.err.unreachable", "connect timeout") from None
        except OSError as exc:
            raise AIError("ai.err.unreachable", type(exc).__name__) from None
        self.sent = self.clock()
        try:
            self.sock.settimeout(limits.first)
            self.conn.request(method, target, body=body, headers=headers)
            self.resp = self.conn.getresponse()
        except socket.timeout:
            self.close()
            raise AIError("ai.err.no_answer", "no first byte within %ds" % limits.first) from None
        except (OSError, http.client.HTTPException) as exc:
            self.close()
            raise AIError("ai.err.unreachable", type(exc).__name__) from None
        self.first = self.clock()
        self.read_bytes = 0
        if self.resp.status >= 400:
            raw = b""
            try:
                raw = self.resp.read(16384)
            except (OSError, http.client.HTTPException):
                pass
            self.close()
            raise _http_error(self.resp.status, raw)

    def _arm(self) -> None:
        left = self.limits.done - (self.clock() - self.first)
        if left <= 0:
            raise AIError("ai.err.timeout", "answer not complete within %ds" % self.limits.done)
        try:
            self.sock.settimeout(max(0.5, min(self.limits.idle, left)))
        except OSError:
            pass          # the answer was complete and http.client already closed the socket: readline() reports the end

    def lines(self):
        while True:
            self._arm()
            try:
                line = self.resp.readline(MAX_BODY)
            except socket.timeout:
                self.close()
                raise AIError("ai.err.timeout", "answer stalled") from None
            except (OSError, http.client.HTTPException) as exc:
                self.close()
                raise AIError("ai.err.unreachable", type(exc).__name__) from None
            if not line:
                return
            self.read_bytes += len(line)
            if self.read_bytes > MAX_BODY:
                self.close()
                raise AIError("ai.err.too_large")
            yield line.decode("utf-8", "replace").rstrip("\r\n")

    def read_all(self) -> bytes:
        out = []
        for line in self.lines():
            out.append(line)
        return "\n".join(out).encode("utf-8")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def _http_error(status: int, raw: bytes) -> AIError:
    text = raw.decode("utf-8", "replace").lower()
    if status in (401, 403):
        return AIError("ai.err.auth", "HTTP %d" % status, status)
    if status == 404 and "model" in text:
        return AIError("ai.err.model", "HTTP 404", status)
    if status == 404:
        return AIError("ai.err.not_found", "HTTP 404", status)
    if status == 429:
        return AIError("ai.err.rate", "HTTP 429", status)
    if status == 400 and "model" in text:
        return AIError("ai.err.model", "HTTP 400", status)
    return AIError("ai.err.http", "HTTP %d" % status, status)


def _sse_data(lines):
    """``data:`` payloads of a server-sent event stream as parsed JSON objects (comments and [DONE] skipped)."""
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except ValueError:
            continue


# ---------------------------------------------------------------------- the adapter
class Client:
    """``Client(kind, url, key)``: ``models()``, ``chat(system, user, model)``, ``test(model)``.
    ``opener`` is the request factory (tests pass a stand-in; the default speaks HTTP)."""

    def __init__(self, kind: str, url: str = "", key=None, limits: Optional[Limits] = None, opener: Callable = _Stream,
                 clock: Callable = time.monotonic):
        self.p = get(kind)
        self.base = normalise_url(kind, url) if self.p.dialect != "demo" else ""
        self.key = key if isinstance(key, Secret) else Secret(key or "")
        if self.p.key == "required" and not self.key:
            raise AIError("ai.err.key_missing")
        self.limits = limits or (LOCAL_LIMITS if self.p.scope == "local" else PUBLIC_LIMITS)
        self.opener = opener
        self.clock = clock

    # -------------------------------------------------------------- request building
    def _headers(self) -> dict:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
             "User-Agent": "lanloom-ai"}
        k = self.key.reveal() if self.key else ""
        if self.p.dialect == "anthropic":
            h["x-api-key"] = k
            h["anthropic-version"] = "2023-06-01"
        elif self.p.dialect == "gemini":
            h["x-goog-api-key"] = k
        elif k:
            h["Authorization"] = "Bearer " + k
        if self.p.kind == "openrouter":
            h["X-Title"] = "Lanloom"
        return h

    def _open(self, method: str, path: str, body=None, limits: Optional[Limits] = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        return self.opener(self.base + path, method, self._headers(), data, limits or self.limits)

    # -------------------------------------------------------------- model list
    def models(self) -> list:
        """Model ids the service offers (``[]`` = reachable but nothing installed). Raises ``AIError``."""
        if self.p.dialect == "demo":
            return ["sample-small", "sample-medium"]
        lim = Limits(connect=min(5.0, self.limits.connect), first=LIST_TIMEOUT, done=LIST_TIMEOUT, idle=LIST_TIMEOUT)
        path = {"native": "/api/tags", "openai": "/models", "anthropic": "/v1/models?limit=100", "gemini": "/models?pageSize=200"}[self.p.dialect]
        st = self._open("GET", path, limits=lim)
        try:
            raw = st.read_all()
        finally:
            st.close()
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise AIError("ai.err.bad_answer") from None
        ids = []
        if self.p.dialect == "native":
            ids = [m.get("name") or m.get("model") for m in data.get("models") or [] if isinstance(m, dict)]
        elif self.p.dialect in ("openai", "anthropic"):
            ids = [m.get("id") for m in data.get("data") or [] if isinstance(m, dict)]
        elif self.p.dialect == "gemini":
            for m in data.get("models") or []:
                if isinstance(m, dict) and "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"]):
                    ids.append(str(m.get("name") or "").split("/", 1)[-1])
        ids = [str(i) for i in ids if i and isinstance(i, str) and len(i) <= 160]
        ids = [i for i in ids if not UNUSABLE_MODEL.search(i)]
        return sorted(set(ids), key=ids.index)[:MAX_MODELS]

    def recommended(self, ids: list, extra_prefer: tuple = ()) -> str:
        for rx in tuple(extra_prefer) + self.p.prefer:
            for i in ids:
                if re.search(rx, i, re.I):
                    return i
        return ids[0] if ids else ""

    # -------------------------------------------------------------- chat
    def chat(self, system: str, user: str, model: str, max_tokens: int = 700, on_first: Callable = None) -> dict:
        """One question, one answer: ``{"text", "ms", "first_ms", "model"}``. Raises ``AIError``."""
        if not model:
            raise AIError("ai.err.model_missing")
        t0 = self.clock()
        if self.p.dialect == "demo":
            from .demo import answer
            text = answer(system, user)
            return {"text": text, "ms": 420, "first_ms": 180, "model": model}
        d = self.p.dialect
        if d == "native":
            body = {"model": model, "stream": True, "keep_alive": "30m",
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "options": {"num_predict": max_tokens, "temperature": 0.3}}
            st = self._open("POST", "/api/chat", body)
        elif d == "openai":
            body = {"model": model, "stream": True,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if self.p.kind == "openai":
                body["max_completion_tokens"] = max(1024, max_tokens)   # reasoning models count their thinking too
            else:
                body["max_tokens"] = max_tokens
                body["temperature"] = 0.3
            st = self._open("POST", "/chat/completions", body)
        elif d == "anthropic":
            body = {"model": model, "stream": True, "max_tokens": max_tokens, "system": system,
                    "messages": [{"role": "user", "content": user}]}
            st = self._open("POST", "/v1/messages", body)
        elif d == "gemini":
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"maxOutputTokens": max(1024, max_tokens), "temperature": 0.3}}
            st = self._open("POST", "/models/%s:streamGenerateContent?alt=sse" % urllib.parse.quote(model, safe=""), body)
        else:
            raise AIError("ai.err.provider")
        first_ms = int((self.clock() - t0) * 1000)
        if on_first:
            on_first()
        parts = []
        try:
            if d == "native":
                for line in st.lines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("error"):
                        raise _model_error(str(obj.get("error")))
                    parts.append(str((obj.get("message") or {}).get("content") or ""))
                    if obj.get("done"):
                        break
                    if sum(map(len, parts)) > MAX_ANSWER_CHARS:
                        break
            else:
                for obj in _sse_data(st.lines()):
                    if isinstance(obj, dict) and obj.get("error"):
                        err = obj.get("error")
                        raise _model_error(str(err.get("message") if isinstance(err, dict) else err))
                    if d == "openai":
                        for ch in obj.get("choices") or []:
                            parts.append(str((ch.get("delta") or {}).get("content") or ""))
                    elif d == "anthropic":
                        if obj.get("type") == "content_block_delta":
                            parts.append(str((obj.get("delta") or {}).get("text") or ""))
                        elif obj.get("type") == "message_stop":
                            break
                    elif d == "gemini":
                        for cand in obj.get("candidates") or []:
                            for part in ((cand.get("content") or {}).get("parts") or []):
                                if not part.get("thought"):
                                    parts.append(str(part.get("text") or ""))
                    if sum(map(len, parts)) > MAX_ANSWER_CHARS:
                        break
        finally:
            st.close()
        text = "".join(parts).strip()
        text = re.sub(r"(?s)<think>.*?</think>", "", text).strip()      # local reasoning models print their thinking
        if not text:
            raise AIError("ai.err.empty")
        return {"text": text[:MAX_ANSWER_CHARS], "ms": int((self.clock() - t0) * 1000), "first_ms": first_ms, "model": model}

    def test(self, model: str) -> dict:
        """A real question with a known answer: success is an answer from the model, not "reachable"."""
        return self.chat("You are a connection test. Answer with one short sentence.",
                         "Reply with the word OK and nothing else.", model, max_tokens=24)


def _model_error(text: str) -> AIError:
    low = text.lower()
    if "not found" in low or "no such model" in low or "does not exist" in low:
        return AIError("ai.err.model", "model not found")
    if "memory" in low:
        return AIError("ai.err.memory", "not enough memory")
    return AIError("ai.err.http", "error from the service")


# ---------------------------------------------------------------------- local server extras (native dialect only)
def native_version(base: str, opener: Callable = _Stream) -> Optional[str]:
    """Version of a local model server, or None when nothing answers there (short limits, never raises)."""
    try:
        st = opener(base + "/api/version", "GET", {"Accept": "application/json"}, None, Limits(2.0, 3.0, 3.0, 3.0))
        try:
            data = json.loads(st.read_all().decode("utf-8") or "{}")
        finally:
            st.close()
        v = data.get("version")
        return str(v)[:40] if v else None
    except (AIError, ValueError, AttributeError):
        return None


def native_pull(base: str, model: str, progress: Callable, opener: Callable = _Stream, deadline: float = 3 * 3600) -> None:
    """Download a model into a local server; ``progress(status, completed, total)``. Raises ``AIError``."""
    lim = Limits(connect=5.0, first=120.0, done=deadline, idle=300.0)
    st = opener(base + "/api/pull", "POST", {"Content-Type": "application/json", "Accept": "application/json"},
                json.dumps({"model": model, "stream": True}).encode("utf-8"), lim)
    ok = False
    try:
        for line in st.lines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("error"):
                raise _model_error(str(obj["error"])) if "not found" not in str(obj["error"]).lower() \
                    else AIError("ai.err.model", "unknown model")
            progress(str(obj.get("status") or "")[:60], int(obj.get("completed") or 0), int(obj.get("total") or 0))
            if obj.get("status") == "success":
                ok = True
    finally:
        st.close()
    if not ok:
        raise AIError("ai.err.pull_incomplete")
