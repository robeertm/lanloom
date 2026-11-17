"""The one send path of an adapter half, with its allow-list checked below the adapter.

An adapter never opens a connection itself. It builds a ``Transport`` with an allow-list and calls
``request(method, path, body)``; the transport refuses anything the list does not name *before* a byte is sent.
The adapter cannot widen the list at run time (lists are frozen when the transport is made).

* ``read_allowlist(...)`` - for read halves: GET only, plus at most a login/logout POST whose body has exactly the
  declared credential fields, plus (for devices whose read API is a query document sent by POST) a *query* POST
  whose every body field has a mandatory value check that proves the document only reads. Anything else in a read
  list is a programming error and raises at import time.
* ``write_allowlist(...)`` - for write halves (executor only): every rule names the method, the path pattern, the
  exact body fields it may carry and a check for each value. A write rule without a body specification is invalid.

Static tests check that read halves and translation modules import no network module and reach the network only
through this file.
"""
import hashlib
import hmac
import http.client
import json
import re
import ssl
from urllib.parse import urlencode, urlsplit

MAX_BYTES = 8 * 1024 * 1024
READ_METHODS = ("GET",)
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


class Forbidden(Exception):
    """A request outside the allow-list was about to be sent - refused, nothing was sent."""


class TransportError(Exception):
    def __init__(self, key: str, detail: str = ""):
        super().__init__(key)
        self.key, self.detail = key, detail


class Rule:
    __slots__ = ("method", "path", "fields", "values", "login", "nonempty", "query", "form", "exact")

    def __init__(self, method: str, path: str, *, fields=None, values=None, login: bool = False, nonempty: bool = False,
                 query: bool = False, form: bool = False, exact: bool = False):
        self.method = method.upper()
        self.path = re.compile(r"\A(?:%s)\Z" % path)
        self.fields = None if fields is None else frozenset(fields)
        self.values = dict(values or {})
        self.login = login
        self.nonempty = nonempty                 # the body must carry at least one of the fields
        self.query = query                       # read query: every field present and checked (see read_allowlist)
        self.form = form                         # body sent as application/x-www-form-urlencoded instead of JSON
        self.exact = exact                       # the body carries every field (a complete, checked object)
        if set(self.values) - set(self.fields or ()):
            raise ValueError("value checks for fields the rule does not allow")
        if query and (not self.fields or set(self.values) != set(self.fields) or not all(callable(c) for c in self.values.values())):
            raise ValueError("a query rule needs a value check for every body field")

    def allows(self, method: str, path: str, body) -> bool:
        if method != self.method or not isinstance(path, str) or not self.path.match(path):
            return False
        if self.fields is None:
            return body is None
        if not isinstance(body, dict) or not set(body) <= self.fields:
            return False
        if (self.login or self.query or self.exact) and set(body) != self.fields:
            return False
        if self.nonempty and not body:
            return False
        for name, check in self.values.items():
            if name in body:
                try:
                    if check(body[name]) is False:
                        return False
                except (TypeError, ValueError):
                    return False
        return True


class AllowList:
    def __init__(self, rules, *, write: bool):
        self.rules = tuple(rules)
        self.write = write
        for r in self.rules:
            if not write:
                if r.method not in READ_METHODS and not (r.method == "POST" and (r.login or r.query or r.fields == frozenset())):
                    raise ValueError("read allow-list may only hold GET, login/logout and checked query rules")
                if r.method == "GET" and r.fields is not None:
                    raise ValueError("GET carries no body")
            elif r.method in WRITE_METHODS and r.method != "DELETE" and r.fields is None:
                raise ValueError("write rule %s needs its exact body fields" % r.path.pattern)

    def check(self, method: str, path: str, body=None) -> "Rule":
        method = (method or "").upper()
        for r in self.rules:
            if r.allows(method, path, body):
                return r
        raise Forbidden("%s %s" % (method[:8], str(path)[:60]))


def _strict(check):
    """A query check that refuses on anything but a plain True (including every exception)."""
    def run(value) -> bool:
        try:
            return check(value) is True
        except Exception:
            return False
    return run


def read_allowlist(get_paths, *, login=None, logout=None, query=None) -> AllowList:
    """``get_paths``: regex patterns; ``login``: (path, credential fields); ``logout``: path (POST without body);
    ``query``: (path, {field: check}) - a form-encoded POST for read APIs that take a query document; ``check(value)``
    must return True only for a document that reads (anything else, or an exception, refuses the request)."""
    rules = [Rule("GET", p) for p in get_paths]
    if query:
        checks = {k: _strict(c) for k, c in dict(query[1]).items()}
        rules.append(Rule("POST", query[0], fields=tuple(checks), values=checks, query=True, form=True))
    if login:
        rules.append(Rule("POST", login[0], fields=login[1], login=True))
    if logout:
        rules.append(Rule("POST", logout, fields=()))
    return AllowList(rules, write=False)


def write_allowlist(rules) -> AllowList:
    return AllowList(rules, write=True)


def _ssl_context(allow_self_signed: bool):
    ctx = ssl.create_default_context()
    if allow_self_signed:              # explicit opt-in only (a device with its own certificate)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class Transport:
    """Blocking HTTP(S) to one device; call from a thread. Headers with credentials come from ``auth_headers``
    (a callable), so this object never stores them and ``repr()`` shows only the host."""

    def __init__(self, base_url: str, allow: AllowList, *, auth_headers=None, allow_self_signed: bool = False,
                 timeout: float = 10.0, max_bytes: int = MAX_BYTES, pin_sha256: str = None):
        parts = urlsplit(str(base_url or ""))
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise TransportError("integration.err.url")
        self._tls = parts.scheme == "https"
        self._host = parts.hostname
        self._port = parts.port or (443 if self._tls else 80)
        self._allow = allow
        self._auth = auth_headers
        self._self_signed = bool(allow_self_signed)
        pin = re.sub(r"[^0-9a-f]", "", str(pin_sha256 or "").lower())
        if pin_sha256 and len(pin) != 64:
            raise TransportError("integration.err.pin")
        self._pin = pin or None                  # SHA-256 of the device's certificate: the connection is bound to it
        self.timeout = max(1.0, float(timeout))
        self.max_bytes = max_bytes
        self.refused = 0
        self.sent = 0

    def __repr__(self):
        return "Transport(%s:%d)" % (self._host, self._port)

    __str__ = __repr__

    @property
    def writes(self) -> bool:
        return self._allow.write

    def request(self, method: str, path: str, body=None, *, headers=None):
        """One allowed request. Returns (status, headers list, bytes)."""
        try:
            rule = self._allow.check(method, path, body)
        except Forbidden:
            self.refused += 1
            raise
        method = method.upper()
        heads = {"Accept": "application/json", "User-Agent": "lanloom", "Connection": "close"}
        if headers:
            heads.update({k: v for k, v in headers.items() if k.lower() not in ("host", "content-length")})
        if self._auth:
            heads.update(self._auth())
        payload = None
        if body is not None and rule.form:
            heads["Content-Type"] = "application/x-www-form-urlencoded"
            payload = urlencode(body).encode("utf-8")
        elif body is not None:
            heads["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        if self._tls:
            conn = http.client.HTTPSConnection(self._host, self._port, timeout=self.timeout,
                                               context=_ssl_context(self._self_signed or bool(self._pin)))
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)
        try:
            try:
                if self._pin and self._tls:
                    conn.connect()
                    seen = hashlib.sha256(conn.sock.getpeercert(binary_form=True) or b"").hexdigest()
                    if not hmac.compare_digest(seen, self._pin):
                        raise TransportError("integration.err.certificate", "certificate does not match the pinned one")
                conn.request(method, path, body=payload, headers=heads)
                resp = conn.getresponse()
                data = resp.read(self.max_bytes + 1)
                status, rheads = resp.status, resp.getheaders()
            except http.client.HTTPException:
                raise TransportError("integration.err.protocol", "malformed HTTP reply") from None
            except OSError as exc:
                raise TransportError("integration.err.unreachable", type(exc).__name__) from None
        finally:
            conn.close()
        self.sent += 1
        if len(data) > self.max_bytes:
            raise TransportError("integration.err.protocol", "reply too large")
        return status, rheads, data

    def json(self, method: str, path: str, body=None, **kw):
        status, _, data = self.request(method, path, body, **kw)
        try:
            return status, (json.loads(data.decode("utf-8")) if data else None)
        except ValueError:
            raise TransportError("integration.err.protocol", "reply is not JSON") from None
