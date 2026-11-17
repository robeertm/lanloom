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

* a console also puts the CSRF token into the payload of its ``TOKEN`` cookie (field ``csrfToken``); it is used when
  no header carried one.

The write half (executor) uses the same session class with a stricter cap that holds across processes and restarts
(``FileLedger``: a small 0600 file in the executor's state directory, never followed through a symbolic link).

Cookies, CSRF token and password stay in private attributes; ``repr()`` shows the controller only.
"""
import base64
import hashlib
import json
import os
import stat
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
            self._csrf = self._cookies.get("csrf_token") or csrf_from_token(self._cookies.get("TOKEN"))

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


def csrf_from_token(token):
    """The ``csrfToken`` field of a console's ``TOKEN`` cookie (a signed token of three dot-separated parts), or None."""
    try:
        part = str(token or "").split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, TypeError):
        return None
    value = data.get("csrfToken") if isinstance(data, dict) else None
    if isinstance(value, str) and 0 < len(value) <= 2048 and all(32 < ord(c) < 127 for c in value):
        return value
    return None


class FileLedger:
    """Sign-in cap of the write half across processes: at most ``per_hour`` sign-ins in any hour, and after a refused
    session ``pause_s`` seconds without any. ``book()`` before every sign-in raises LoginPaused when it must wait.
    An unreadable or tampered file counts as a fresh refusal (rather wait than flood the controller)."""

    def __init__(self, path, per_hour: int = 4, pause_s: int = PAUSE_AFTER_REFUSAL_S, clock=time.time):
        self.path, self.per_hour, self.pause_s, self.clock = (str(path) if path else None), per_hour, pause_s, clock
        self._mem = {"logins": [], "not_before": 0.0}
        self._lock = threading.Lock()

    def __repr__(self):
        return "FileLedger(per_hour=%d)" % self.per_hour

    def _read(self) -> dict:
        if not self.path:
            return dict(self._mem)
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {"logins": [], "not_before": 0.0}
        except OSError:
            return {"logins": [], "not_before": self.clock() + self.pause_s}
        try:
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    raise ValueError("not a file")
                data = json.loads(fh.read(65536).decode("utf-8"))
            return {"logins": [float(x) for x in data.get("logins") or [] if isinstance(x, (int, float)) and not isinstance(x, bool)],
                    "not_before": float(data.get("not_before") or 0)}
        except (ValueError, TypeError, AttributeError, OSError):
            return {"logins": [], "not_before": self.clock() + self.pause_s}

    def _write(self, data: dict) -> None:
        if not self.path:
            self._mem = data
            return
        tmp = self.path + ".tmp"
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)

    def book(self) -> None:
        with self._lock:
            now = self.clock()
            data = self._read()
            data["logins"] = [t for t in data["logins"] if now - 3600 < t <= now + 60]
            if now < data["not_before"] or len(data["logins"]) >= self.per_hour:
                self._write(data)
                raise LoginPaused()
            data["logins"].append(now)
            self._write(data)

    def refused(self) -> None:
        with self._lock:
            data = self._read()
            data["not_before"] = self.clock() + self.pause_s
            self._write(data)

    def paused_until(self) -> float:
        with self._lock:
            return self._read()["not_before"]


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
