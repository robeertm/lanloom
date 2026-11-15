"""Session of the classic API (local view-only account): sign-in, cookies, CSRF token, sign-in cap.

Lessons kept from earlier controller work:
* only ``name=value`` of every Set-Cookie is kept (attributes such as ``Partitioned`` are ignored on purpose, some
  Python releases choke on them); the cookies go back as one ``Cookie`` header;
* the CSRF token comes from ``X-CSRF-Token`` or ``X-Updated-CSRF-Token`` (or a ``csrf_token`` cookie) and is sent
  back as ``X-CSRF-Token``;
* after HTTP 401 the read half signs in again once per poll; after 403 it never does (the account lacks a right,
  signing in again cannot help);
* sign-ins are capped per controller and account across all reader objects of this process, and a refused sign-in
  pauses further attempts, so a wrong password can never lock the account through a flood of attempts;
* a session lives in a process-wide cache keyed by controller and account, so the account check, the connection
  test and the running reader share one sign-in.

Cookies, CSRF token and password stay in private attributes; ``repr()`` shows the controller only.
"""
import hashlib
import threading
import time

MAX_COOKIES = 20
MAX_COOKIE_LEN = 4096
LOGINS_PER_HOUR = 6
PAUSE_AFTER_REFUSAL_S = 900
SESSION_MAX_AGE_S = 90 * 60              # sign in again before the controller's own session limit

_LOCK = threading.Lock()
_LEDGER = {}                             # key -> [sign-in times], pause-until
_SESSIONS = {}                           # key -> Session


class LoginPaused(Exception):
    """Too many sign-ins recently, or the last one was refused: wait before trying again."""


def _key(host: str, user: str, secret: str) -> str:
    return hashlib.sha256(("%s\0%s\0%s" % (host, user, secret)).encode("utf-8")).hexdigest()


class Session:
    def __init__(self, key: str):
        self._key = key
        self._cookies = {}
        self._csrf = None
        self.style = None                 # "console" | "classic" once signed in
        self.since = 0.0

    def __repr__(self):
        return "Session(signed_in=%s)" % bool(self._cookies)

    @property
    def signed_in(self) -> bool:
        return bool(self._cookies) and time.time() - self.since < SESSION_MAX_AGE_S

    def forget(self) -> None:
        self._cookies.clear()
        self._csrf = None
        self.since = 0.0

    def absorb(self, heads) -> None:
        for key, value in heads or ():
            k = key.lower()
            if k == "set-cookie":
                pair = value.split(";", 1)[0].strip()
                name, sep, val = pair.partition("=")
                if (not sep or not name or len(pair) > MAX_COOKIE_LEN
                        or any(ord(c) < 33 or ord(c) > 126 or c in ";," for c in name + val)):
                    continue
                if not val:
                    self._cookies.pop(name, None)
                elif name in self._cookies or len(self._cookies) < MAX_COOKIES:
                    self._cookies[name] = val
            elif k in ("x-csrf-token", "x-updated-csrf-token"):
                if value and len(value) <= 2048 and all(32 < ord(c) < 127 for c in value):
                    self._csrf = value
        if not self._csrf:
            self._csrf = self._cookies.get("csrf_token")

    def headers(self) -> dict:
        out = {}
        if self._cookies:
            out["Cookie"] = "; ".join("%s=%s" % kv for kv in self._cookies.items())
        if self._csrf:
            out["X-CSRF-Token"] = self._csrf
        return out

    def started(self, style: str) -> None:
        self.style = style
        self.since = time.time()


def shared(host: str, user: str, secret: str) -> Session:
    key = _key(host, user, secret)
    with _LOCK:
        s = _SESSIONS.get(key)
        if s is None:
            s = _SESSIONS[key] = Session(key)
        return s


def may_sign_in(session: Session) -> None:
    """Raises LoginPaused when the cap is reached or a refusal pause runs; records the attempt otherwise."""
    now = time.time()
    with _LOCK:
        times, pause = _LEDGER.get(session._key, ([], 0.0))
        times = [t for t in times if now - t < 3600]
        if now < pause or len(times) >= LOGINS_PER_HOUR:
            _LEDGER[session._key] = (times, pause)
            raise LoginPaused()
        times.append(now)
        _LEDGER[session._key] = (times, pause)


def refused(session: Session) -> None:
    with _LOCK:
        times, _ = _LEDGER.get(session._key, ([], 0.0))
        _LEDGER[session._key] = (times, time.time() + PAUSE_AFTER_REFUSAL_S)


def _reset_for_tests() -> None:
    with _LOCK:
        _LEDGER.clear()
        _SESSIONS.clear()
