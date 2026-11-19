"""The one send path of an adapter half, with its allow-list checked below the adapter.

An adapter never opens a connection itself. It builds a ``Transport`` with an allow-list and calls
``request(method, path, body)``; the transport refuses anything the list does not name *before* a byte is sent.
The adapter cannot widen the list at run time (lists are frozen when the transport is made).

* ``read_allowlist(...)`` - for read halves: GET only, plus at most a login/logout POST whose body has exactly the
  declared credential fields, plus (for devices whose read API is a query document sent by POST) a *query* POST
  whose every body field has a mandatory value check that proves the document only reads - form-encoded
  (``query``) or as a JSON object (``json_query``, e.g. a JSON-RPC call whose object, method and arguments are
  checked), plus (for devices whose read API is SOAP) a *soap* POST: one control path, a fixed table of the
  (service, action) pairs it may call and, for each, the exact argument names with a mandatory value check - the
  transport itself builds the envelope from that checked table row, the adapter never hands over XML. Anything else
  in a read list is a programming error and raises at import time.
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
import secrets
import ssl
import threading
from urllib.parse import urlencode, urlsplit
from xml.sax.saxutils import escape as _xml_escape

MAX_BYTES = 8 * 1024 * 1024
READ_METHODS = ("GET",)
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


SOAP_NAME = re.compile(r"\A[A-Za-z][A-Za-z0-9:_.-]{0,95}\Z")
SOAP_READ_ACTION = re.compile(r"\A(?:X_[A-Za-z]{1,8}-[A-Za-z]{1,8}_)?Get[A-Za-z0-9]{0,64}\Z")      # Get..., Vendor extension X_AVM-DE_Get...


class Forbidden(Exception):
    """A request outside the allow-list was about to be sent - refused, nothing was sent."""


class TransportError(Exception):
    def __init__(self, key: str, detail: str = ""):
        super().__init__(key)
        self.key, self.detail = key, detail


class Rule:
    __slots__ = ("method", "path", "fields", "values", "login", "nonempty", "query", "form", "exact", "soap")

    def __init__(self, method: str, path: str, *, fields=None, values=None, login: bool = False, nonempty: bool = False,
                 query: bool = False, form: bool = False, exact: bool = False, soap=None):
        self.method = method.upper()
        self.path = re.compile(r"\A(?:%s)\Z" % path)
        self.fields = None if fields is None else frozenset(fields)
        self.values = dict(values or {})
        self.login = login
        self.nonempty = nonempty                 # the body must carry at least one of the fields
        self.query = query                       # read query: every field present and checked (see read_allowlist)
        self.form = form                         # body sent as application/x-www-form-urlencoded instead of JSON
        self.exact = exact                       # the body carries every field (a complete, checked object)
        self.soap = None if soap is None else {k: dict(v) for k, v in dict(soap).items()}   # (service, action) -> {argument: check}
        if self.soap is not None:
            if self.method != "POST" or fields is not None or not self.soap:
                raise ValueError("a soap rule is a POST with a table of calls and no other body specification")
            for (service, action), args in self.soap.items():
                if not SOAP_NAME.match(service) or not SOAP_READ_ACTION.match(action) \
                        or not all(SOAP_NAME.match(a) and callable(c) for a, c in args.items()):
                    raise ValueError("a read soap call is a Get* action with checked arguments")
        if set(self.values) - set(self.fields or ()):
            raise ValueError("value checks for fields the rule does not allow")
        if query and (not self.fields or set(self.values) != set(self.fields) or not all(callable(c) for c in self.values.values())):
            raise ValueError("a query rule needs a value check for every body field")

    def _allows_soap(self, body) -> bool:
        if not isinstance(body, dict) or set(body) != {"service", "action", "args"} or not isinstance(body["args"], dict):
            return False
        row = self.soap.get((body["service"], body["action"])) if isinstance(body["service"], str) and isinstance(body["action"], str) else None
        if row is None or set(body["args"]) != set(row):
            return False
        for name, check in row.items():
            try:
                if check(body["args"][name]) is not True:
                    return False
            except Exception:
                return False
        return True

    def allows(self, method: str, path: str, body) -> bool:
        if method != self.method or not isinstance(path, str) or not self.path.match(path):
            return False
        if self.soap is not None:
            return self._allows_soap(body)
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
                if r.method not in READ_METHODS and not (r.method == "POST" and (r.login or r.query or r.soap is not None
                                                                                 or r.fields == frozenset())):
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


def read_allowlist(get_paths, *, login=None, logout=None, query=None, json_query=None, soap=None) -> AllowList:
    """``get_paths``: regex patterns; ``login``: (path, credential fields); ``logout``: path (POST without body);
    ``query``: (path, {field: check}) - a form-encoded POST for read APIs that take a query document; ``json_query``:
    the same with the document sent as a JSON object; ``check(value)`` must return True only for a document that reads
    (anything else, or an exception, refuses the request). Every field of a query is present and checked.
    ``soap``: [(control path pattern, {(service, action): {argument: check}})]; the call must be a table row and every
    argument of the row present and checked; the transport builds the envelope and answers a digest challenge."""
    rules = [Rule("GET", p) for p in get_paths]
    for spec, form in ((query, True), (json_query, False)):
        if spec:
            checks = {k: _strict(c) for k, c in dict(spec[1]).items()}
            rules.append(Rule("POST", spec[0], fields=tuple(checks), values=checks, query=True, form=form))
    for path, table in soap or ():
        rules.append(Rule("POST", path, soap={k: {a: _strict(c) for a, c in v.items()} for k, v in dict(table).items()}))
    if login:
        rules.append(Rule("POST", login[0], fields=login[1], login=True))
    if logout:
        rules.append(Rule("POST", logout, fields=()))
    return AllowList(rules, write=False)


def write_allowlist(rules) -> AllowList:
    return AllowList(rules, write=True)


def _soap_envelope(service: str, action: str, args: dict) -> bytes:
    """The SOAP envelope of one checked call (names are validated identifiers, argument values are escaped)."""
    inner = "".join("<%s>%s</%s>" % (k, _xml_escape(str(v)), k) for k, v in args.items())
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            '<s:Body><u:%s xmlns:u="%s">%s</u:%s></s:Body></s:Envelope>' % (action, _xml_escape(service), inner, action)).encode("utf-8")


_CHAL_PAIR = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:"([^"]{0,512})"|([^\s,"]{1,128}))')


def _parse_challenge(rheads):
    """The digest challenge of a 401 reply as a dict (realm, nonce, opaque, algorithm MD5|SHA-256, qop 'auth' or ''),
    or None when there is none this transport can answer."""
    for name, value in rheads or ():
        if name.lower() != "www-authenticate" or not str(value).lower().lstrip().startswith("digest"):
            continue
        pairs = {k.lower(): q or v for k, q, v in _CHAL_PAIR.findall(str(value))}
        algo = (pairs.get("algorithm") or "MD5").upper()
        qops = [x.strip() for x in (pairs.get("qop") or "").lower().split(",")]
        if algo not in ("MD5", "SHA-256") or not pairs.get("nonce") or "realm" not in pairs or '"' in pairs["realm"]:
            continue
        if pairs.get("qop") and "auth" not in qops:
            continue
        return {"realm": pairs["realm"], "nonce": pairs["nonce"], "opaque": pairs.get("opaque") or "", "algorithm": algo,
                "qop": "auth" if "auth" in qops else ""}
    return None


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
                 timeout: float = 10.0, max_bytes: int = MAX_BYTES, pin_sha256: str = None, digest=None):
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
        self._digest = digest                    # callable -> (user name, password) for soap calls; never stored here
        self._chal = None                        # the device's last digest challenge (realm, nonce, ...)
        self._nc = 0
        self._dlock = threading.Lock()
        self.challenges = 0                      # sign-ins answered from a fresh challenge (the reader caps them)

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
        if rule.soap is not None:
            payload = _soap_envelope(body["service"], body["action"], body["args"])
            heads.update({"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": '"%s#%s"' % (body["service"], body["action"]),
                          "Accept": "text/xml, application/xml"})
        elif body is not None and rule.form:
            heads["Content-Type"] = "application/x-www-form-urlencoded"
            payload = urlencode(body).encode("utf-8")
        elif body is not None:
            heads["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        status, rheads, data = self._exchange(method, path, payload, heads, bool(rule.soap is not None and self._digest))
        self.sent += 1
        if len(data) > self.max_bytes:
            raise TransportError("integration.err.protocol", "reply too large")
        return status, rheads, data

    def _exchange(self, method: str, path: str, payload, heads: dict, digest: bool):
        """One HTTP exchange; with ``digest`` the device's challenge is answered (cached, one retry on a fresh one)."""
        if digest:
            with self._dlock:
                chal = self._chal
                if chal:
                    self._nc += 1
                    heads = dict(heads, Authorization=self._digest_header(method, path, chal, self._nc))
        status, rheads, data = self._once(method, path, payload, heads)
        if digest and status == 401:
            fresh = _parse_challenge(rheads)
            if fresh:
                with self._dlock:
                    self._chal, self._nc = fresh, 1
                    self.challenges += 1
                    heads = dict(heads, Authorization=self._digest_header(method, path, fresh, 1))
                status, rheads, data = self._once(method, path, payload, heads)
                if status == 401:
                    with self._dlock:
                        self._chal = None                   # refused with fresh credentials: nothing worth keeping
        return status, rheads, data

    def _digest_header(self, method: str, path: str, chal: dict, nc: int) -> str:
        user, password = self._digest()
        user, password = str(user), str(password)
        if '"' in user or any(ord(c) < 32 for c in user):
            raise TransportError("integration.err.protocol", "user name cannot be sent")
        algo = chal["algorithm"]
        h = (lambda x: hashlib.sha256(x.encode("utf-8")).hexdigest()) if algo == "SHA-256" else \
            (lambda x: hashlib.md5(x.encode("utf-8"), usedforsecurity=False).hexdigest())      # the device's challenge decides
        ha1 = h("%s:%s:%s" % (user, chal["realm"], password))
        ha2 = h("%s:%s" % (method, path))
        cnonce = secrets.token_hex(8)
        if chal["qop"]:
            response = h("%s:%s:%08x:%s:auth:%s" % (ha1, chal["nonce"], nc, cnonce, ha2))
        else:
            response = h("%s:%s:%s" % (ha1, chal["nonce"], ha2))
        parts = ['username="%s"' % user, 'realm="%s"' % chal["realm"], 'nonce="%s"' % chal["nonce"], 'uri="%s"' % path,
                 'response="%s"' % response, "algorithm=%s" % algo]
        if chal["qop"]:
            parts += ["qop=auth", "nc=%08x" % nc, 'cnonce="%s"' % cnonce]
        if chal["opaque"]:
            parts.append('opaque="%s"' % chal["opaque"])
        return "Digest " + ", ".join(parts)

    def _once(self, method: str, path: str, payload, heads: dict):
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
                return resp.status, resp.getheaders(), data
            except http.client.HTTPException:
                raise TransportError("integration.err.protocol", "malformed HTTP reply") from None
            except OSError as exc:
                raise TransportError("integration.err.unreachable", type(exc).__name__) from None
        finally:
            conn.close()

    def json(self, method: str, path: str, body=None, **kw):
        status, _, data = self.request(method, path, body, **kw)
        try:
            return status, (json.loads(data.decode("utf-8")) if data else None)
        except ValueError:
            raise TransportError("integration.err.protocol", "reply is not JSON") from None
