"""API tokens: one person's key for scripts and other programs that use the public API (``/api/v1/...``).

* A token belongs to one account and carries a scope: ``read`` (like a viewer), ``operate`` (like an operator),
  ``admin`` (like an administrator), or ``ingest`` (may only deliver an external report, reads nothing). The scope can
  never be more than the account's role - neither when the token is made nor later: a token of an account that was
  demoted works with the lower role at once, a token of a disabled or deleted account stops working.
* Every token expires (at most one year). It can be revoked at any time by its owner or an administrator.
* Only a SHA-256 hash of the token is stored, together with the first characters for recognising it. The token itself
  is shown once, when it is made. It is never written to the log, the audit log, a backup or a diagnostics export.
* "Last used" (time and address) is kept per token; each token has its own request limit per minute.
* A token opens only the public API: the internal routes of the web page (users, tokens, settings, ...) never accept one.

Token format: ``llt_`` + 43 characters (32 random bytes, URL-safe base64).
"""
import hashlib
import secrets
import threading
import time
from collections import deque
from typing import Callable, Optional

from . import access

PREFIX = "llt_"
SCOPES = ("read", "operate", "admin", "ingest")
# scope -> role the token acts with (before the account's own role caps it); "ingest" acts with no role at all
SCOPE_ROLE = {"read": "viewer", "operate": "operator", "admin": "admin", "ingest": None}
# lowest account role that may hold a token of this scope
SCOPE_NEEDS = {"read": "viewer", "operate": "operator", "admin": "admin", "ingest": "operator"}
EXPIRY_DAYS = (7, 30, 90, 365)
DEFAULT_EXPIRY_DAYS = 90
RATES = (60, 300, 1200)            # requests per minute and token
DEFAULT_RATE = 300
MAX_PER_USER = 25
NAME_MAX = 60
SHOWN = 8                          # characters after the prefix kept for recognising a token
LAST_USED_EVERY_S = 60.0           # "last used" is written at most this often per token
WINDOW_S = 60.0


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_bearer(header: str) -> Optional[str]:
    """The API token of an ``Authorization: Bearer llt_...`` header, else None."""
    if not header:
        return None
    parts = header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    tok = parts[1].strip()
    if not tok.startswith(PREFIX) or len(tok) > 120:
        return None
    return tok


def allowed_scopes(role: str) -> list:
    return [s for s in SCOPES if access.allows(role, SCOPE_NEEDS[s])]


def effective_role(scope: str, account_role: str) -> Optional[str]:
    """The role a token acts with: its scope's role, capped by the account's current role."""
    want = SCOPE_ROLE.get(scope)
    if want is None or account_role not in access.RANK:
        return None
    return want if access.rank(want) <= access.rank(account_role) else account_role


class TokenError(Exception):
    def __init__(self, key: str, status: int = 401, wait: float = 0.0):
        super().__init__(key)
        self.key, self.status, self.wait = key, status, wait


class Tokens:
    def __init__(self, store, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock
        self._lock = threading.Lock()
        self._hits: dict = {}                  # token id -> deque of monotonic time stamps
        self._written: dict = {}               # token id -> last time "last used" was written
        self.monotonic = time.monotonic

    # ------------------------------------------------------------------ managing
    def create(self, user: dict, name: str, scope: str, expires_days: int = DEFAULT_EXPIRY_DAYS,
               rate: int = DEFAULT_RATE) -> dict:
        """Make a token for ``user`` (``{"id", "role"}``). Returns the view plus ``token`` (shown once). Raises ValueError."""
        name = " ".join(str(name or "").split())[:NAME_MAX]
        if not name:
            raise ValueError("api.err.name")
        if scope not in SCOPES:
            raise ValueError("api.err.scope")
        if scope not in allowed_scopes(user.get("role")):
            raise ValueError("api.err.scope_role")
        try:
            days = int(expires_days)
            rate = int(rate)
        except (TypeError, ValueError):
            raise ValueError("api.err.expiry")
        if days not in EXPIRY_DAYS:
            raise ValueError("api.err.expiry")
        if rate not in RATES:
            raise ValueError("api.err.rate")
        n = self.store.one("SELECT COUNT(*) AS n FROM api_tokens WHERE user_id=?", (user["id"],))["n"]
        if n >= MAX_PER_USER:
            raise ValueError("api.err.too_many")
        token = PREFIX + secrets.token_urlsafe(32)
        now = self.clock()
        cur = self.store.execute(
            "INSERT INTO api_tokens(user_id, name, scope, prefix, token_hash, created, expires, rate) VALUES(?,?,?,?,?,?,?,?)",
            (user["id"], name, scope, token[:len(PREFIX) + SHOWN], hash_token(token), now, now + days * 86400, rate))
        out = self.view(self.store.one("SELECT * FROM api_tokens WHERE id=?", (cur.lastrowid,)))
        out["token"] = token
        return out

    def view(self, row: dict, owner: Optional[str] = None) -> dict:
        now = self.clock()
        out = {"id": row["id"], "name": row["name"], "scope": row["scope"], "prefix": row["prefix"], "created": row["created"],
               "expires": row["expires"], "expired": row["expires"] < now, "rate": row["rate"], "last_used": row["last_used"],
               "last_ip": row["last_ip"] or None, "uses": row["uses"]}
        if owner is not None:
            out["owner"] = owner
        return out

    def list(self, user_id: int) -> list:
        return [self.view(r) for r in self.store.query("SELECT * FROM api_tokens WHERE user_id=? ORDER BY created DESC", (user_id,))]

    def list_all(self, except_user: Optional[int] = None) -> list:
        rows = self.store.query("SELECT t.*, u.username FROM api_tokens t JOIN users u ON u.id=t.user_id"
                                " WHERE t.user_id != ? ORDER BY u.username COLLATE NOCASE, t.created DESC", (except_user or -1,))
        return [self.view(r, owner=r["username"]) for r in rows]

    def get(self, token_id: int) -> Optional[dict]:
        return self.store.one("SELECT * FROM api_tokens WHERE id=?", (int(token_id),))

    def revoke(self, token_id: int, user: dict) -> Optional[dict]:
        """Revoke a token of ``user`` (an administrator may revoke any). Returns the revoked row or None."""
        row = self.get(token_id)
        if not row:
            return None
        if row["user_id"] != user.get("id") and not access.allows(user.get("role"), "admin"):
            return None
        self.store.execute("DELETE FROM api_tokens WHERE id=?", (row["id"],))
        with self._lock:
            self._hits.pop(row["id"], None)
            self._written.pop(row["id"], None)
        return row

    def expiring(self, within_s: float, now: Optional[float] = None) -> list:
        now = self.clock() if now is None else now
        return self.store.query("SELECT t.id, t.name, t.expires, u.username FROM api_tokens t JOIN users u ON u.id=t.user_id"
                                " WHERE t.expires > ? AND t.expires <= ? ORDER BY t.expires", (now, now + within_s))

    def expired_since(self, since: float, until: float) -> list:
        return self.store.query("SELECT t.id, t.name, t.expires, u.username FROM api_tokens t JOIN users u ON u.id=t.user_id"
                                " WHERE t.expires > ? AND t.expires <= ? ORDER BY t.expires", (since, until))

    def count(self) -> int:
        return self.store.one("SELECT COUNT(*) AS n FROM api_tokens")["n"]

    def prune(self, keep_expired_days: int = 30) -> int:
        """Tokens that expired more than a month ago go (they can never work again; the audit log keeps the history)."""
        return self.store.execute("DELETE FROM api_tokens WHERE expires < ?", (self.clock() - keep_expired_days * 86400,)).rowcount

    # ------------------------------------------------------------------ using
    def authenticate(self, token: str, ip: str = "") -> dict:
        """The acting user of a request with this token. Raises TokenError (401 unknown/expired, 429 too many requests)."""
        row = self.store.one(
            "SELECT t.id, t.user_id, t.name, t.scope, t.expires, t.rate, t.last_used, u.username, u.role, u.disabled"
            " FROM api_tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=?", (hash_token(token),))
        now = self.clock()
        if not row or row["disabled"]:
            raise TokenError("api.err.token")
        if row["expires"] < now:
            raise TokenError("api.err.token_expired")
        wait = self._hit(row["id"], int(row["rate"] or DEFAULT_RATE))
        if wait:
            raise TokenError("api.err.rate", 429, wait)
        self._touch(row["id"], now, ip)
        return {"id": row["user_id"], "username": row["username"], "role": effective_role(row["scope"], row["role"]),
                "account_role": row["role"], "stage": "full", "source": "token",
                "token": {"id": row["id"], "name": row["name"], "scope": row["scope"]}}

    def _hit(self, tid: int, limit: int) -> float:
        now = self.monotonic()
        with self._lock:
            q = self._hits.get(tid)
            if q is None:
                if len(self._hits) > 5000:
                    self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < WINDOW_S}
                q = self._hits[tid] = deque()
            while q and now - q[0] >= WINDOW_S:
                q.popleft()
            if len(q) >= limit:
                return max(0.001, WINDOW_S - (now - q[0]))
            q.append(now)
            return 0.0

    def _touch(self, tid: int, now: float, ip: str) -> None:
        with self._lock:
            pending = self._written.get(tid)
            due = pending is None or now - pending[0] >= LAST_USED_EVERY_S
            if due:
                uses = (pending[1] if pending else 0) + 1
                self._written[tid] = (now, 0)
            else:
                self._written[tid] = (pending[0], pending[1] + 1)
        if due:
            self.store.execute("UPDATE api_tokens SET last_used=?, last_ip=?, uses=uses+? WHERE id=?", (now, str(ip or "")[:64], uses, tid))
