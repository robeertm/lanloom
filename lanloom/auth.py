"""Accounts, sessions, passkeys and request guards.

* Sign-in is mandatory; there is no open mode.
* Passwords: argon2id. Sessions: random 256-bit token in an HttpOnly, SameSite=Strict cookie; only its hash is stored.
* First run: the first account can only be created with the one-time setup code printed to the container log
  (and written to <data>/setup-code.txt). Whoever reaches the page first cannot take over the instance without it.
* State-changing requests need the header `X-Lanloom: 1` (forces a CORS preflight, so other sites cannot send it)
  and, when the browser sends an Origin header, an origin matching the Host header or LANLOOM_TRUSTED_ORIGINS.
* Sign-in attempts are rate limited per client address.
* Passkeys (WebAuthn) are offered in secure contexts (HTTPS or localhost) in addition to the password.
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .store import Store

SESSION_COOKIE = "ll_session"
SESSION_DAYS = 30
SESSION_IDLE_DAYS = 14
MIN_PASSWORD = 10
USERNAME_MAX = 64

_hasher = PasswordHasher()   # argon2id, library defaults (memory-hard)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------- setup code
class SetupCode:
    """One-time code that authorises creation of the first account."""

    def __init__(self, data_dir: Path, store: Store):
        self.path = data_dir / "setup-code.txt"
        self.store = store
        self.code: Optional[str] = None

    def ensure(self) -> Optional[str]:
        if self.store.user_count() > 0:
            self.clear()
            return None
        if self.code is None:
            try:
                existing = self.path.read_text(encoding="ascii").strip()
            except OSError:
                existing = ""
            if len(existing) == 9 and existing[4] == "-":
                self.code = existing
            else:
                raw = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
                self.code = raw[:4] + "-" + raw[4:]
                try:
                    fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w", encoding="ascii") as fh:
                        fh.write(self.code + "\n")
                except OSError:
                    pass
        return self.code

    def check(self, candidate: str) -> bool:
        code = self.ensure()
        if not code:
            return False
        norm = (candidate or "").strip().upper().replace(" ", "")
        if len(norm) == 8:
            norm = norm[:4] + "-" + norm[4:]
        return hmac.compare_digest(norm.encode(), code.encode())

    def clear(self) -> None:
        self.code = None
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------- rate limit
class LoginLimiter:
    """Per-client failure counter with growing lock-out (5 failures -> 60 s, doubling, max 1 h)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}   # client -> [failures, locked_until]

    def wait_seconds(self, client: str) -> float:
        with self._lock:
            entry = self._state.get(client)
            if not entry:
                return 0.0
            return max(0.0, entry[1] - time.time())

    def failure(self, client: str) -> None:
        with self._lock:
            entry = self._state.setdefault(client, [0, 0.0])
            entry[0] += 1
            if entry[0] >= 5:
                entry[1] = time.time() + min(3600, 60 * 2 ** (entry[0] - 5))
            if len(self._state) > 10000:
                self._state.clear()

    def success(self, client: str) -> None:
        with self._lock:
            self._state.pop(client, None)


# ---------------------------------------------------------------------------- accounts
class Accounts:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def validate(username: str, password: str) -> Optional[str]:
        username = (username or "").strip()
        if not username or len(username) > USERNAME_MAX or any(c.isspace() for c in username):
            return "auth.err.username"
        if len(password or "") < MIN_PASSWORD:
            return "auth.err.password_short"
        return None

    def create(self, username: str, password: str) -> int:
        cur = self.store.execute(
            "INSERT INTO users(username, pw_hash, user_handle, created) VALUES(?,?,?,?)",
            (username.strip(), _hasher.hash(password), secrets.token_bytes(32), time.time()),
        )
        return cur.lastrowid

    def verify(self, username: str, password: str) -> Optional[dict]:
        row = self.store.one("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
        if not row:
            # same cost as a real check, so timing does not reveal valid names
            try:
                _hasher.verify(_DUMMY_HASH, password or "")
            except (VerificationError, InvalidHashError):
                pass
            return None
        try:
            _hasher.verify(row["pw_hash"], password or "")
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return None
        if _hasher.check_needs_rehash(row["pw_hash"]):
            self.store.execute("UPDATE users SET pw_hash=? WHERE id=?", (_hasher.hash(password), row["id"]))
        return row

    def set_password(self, user_id: int, password: str) -> None:
        self.store.execute("UPDATE users SET pw_hash=? WHERE id=?", (_hasher.hash(password), user_id))
        self.store.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def get(self, user_id: int) -> Optional[dict]:
        return self.store.one("SELECT id, username, user_handle, created FROM users WHERE id=?", (user_id,))


_DUMMY_HASH = _hasher.hash("lanloom-timing-equaliser")


# ---------------------------------------------------------------------------- sessions
class Sessions:
    def __init__(self, store: Store):
        self.store = store

    def create(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.store.execute(
            "INSERT INTO sessions(token_hash, user_id, created, last_seen, expires) VALUES(?,?,?,?,?)",
            (_hash_token(token), user_id, now, now, now + SESSION_DAYS * 86400),
        )
        self.store.execute("DELETE FROM sessions WHERE expires < ? OR last_seen < ?", (now, now - SESSION_IDLE_DAYS * 86400))
        return token

    def resolve(self, token: Optional[str]) -> Optional[dict]:
        if not token or len(token) > 128:
            return None
        row = self.store.one(
            "SELECT s.token_hash, s.user_id, s.expires, s.last_seen, u.username FROM sessions s JOIN users u ON u.id=s.user_id"
            " WHERE s.token_hash=?",
            (_hash_token(token),),
        )
        now = time.time()
        if not row or row["expires"] < now or row["last_seen"] < now - SESSION_IDLE_DAYS * 86400:
            return None
        if now - row["last_seen"] > 300:
            self.store.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, row["token_hash"]))
        return {"id": row["user_id"], "username": row["username"]}

    def destroy(self, token: Optional[str]) -> None:
        if token:
            self.store.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


# ---------------------------------------------------------------------------- origin guard
def origin_allowed(origin: Optional[str], host: Optional[str], trusted: tuple) -> bool:
    """True when the Origin header (if any) belongs to this site."""
    if not origin:
        return True          # same-origin navigations and non-browser clients send none; the custom header still applies
    origin = origin.rstrip("/")
    if origin in trusted:
        return True
    if not host:
        return False
    for scheme in ("http://", "https://"):
        if origin == scheme + host:
            return True
    return False


# ---------------------------------------------------------------------------- passkeys
class Passkeys:
    """WebAuthn registration and sign-in (discoverable credentials)."""

    TTL = 180

    def __init__(self, store: Store):
        self.store = store
        self._pending = {}   # ceremony id -> (challenge, user_id|None, rp_id, origin, created)
        self._lock = threading.Lock()

    def _remember(self, challenge: bytes, user_id: Optional[int], rp_id: str, origin: str) -> str:
        cid = secrets.token_urlsafe(16)
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._pending.items() if now - v[4] > self.TTL]:
                self._pending.pop(k, None)
            self._pending[cid] = (challenge, user_id, rp_id, origin, now)
        return cid

    def _take(self, cid: str):
        with self._lock:
            entry = self._pending.pop(cid or "", None)
        if not entry or time.time() - entry[4] > self.TTL:
            return None
        return entry

    def list(self, user_id: int) -> list:
        return self.store.query("SELECT id, name, created, last_used FROM passkeys WHERE user_id=? ORDER BY created", (user_id,))

    def delete(self, user_id: int, key_id: int) -> bool:
        return self.store.execute("DELETE FROM passkeys WHERE id=? AND user_id=?", (key_id, user_id)).rowcount > 0

    def registration_options(self, user: dict, rp_id: str, origin: str) -> dict:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                              ResidentKeyRequirement, UserVerificationRequirement)
        existing = [PublicKeyCredentialDescriptor(id=r["credential_id"])
                    for r in self.store.query("SELECT credential_id FROM passkeys WHERE user_id=?", (user["id"],))]
        opts = generate_registration_options(
            rp_id=rp_id, rp_name="Lanloom", user_id=user["user_handle"], user_name=user["username"],
            exclude_credentials=existing, timeout=120000,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.PREFERRED),
        )
        cid = self._remember(opts.challenge, user["id"], rp_id, origin)
        return {"ceremony": cid, "options": json.loads(options_to_json(opts))}

    def registration_verify(self, user_id: int, ceremony: str, credential: dict, name: str) -> bool:
        from webauthn import verify_registration_response
        entry = self._take(ceremony)
        if not entry or entry[1] != user_id:
            return False
        try:
            result = verify_registration_response(credential=credential, expected_challenge=entry[0],
                                                  expected_rp_id=entry[2], expected_origin=entry[3])
        except Exception:   # library raises several types for invalid responses
            return False
        self.store.execute(
            "INSERT INTO passkeys(user_id, credential_id, public_key, sign_count, name, created) VALUES(?,?,?,?,?,?)",
            (user_id, result.credential_id, result.credential_public_key, result.sign_count,
             (name or "Passkey").strip()[:60] or "Passkey", time.time()),
        )
        return True

    def login_options(self, rp_id: str, origin: str) -> dict:
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import UserVerificationRequirement
        opts = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.PREFERRED, timeout=120000)
        cid = self._remember(opts.challenge, None, rp_id, origin)
        return {"ceremony": cid, "options": json.loads(options_to_json(opts))}

    def login_verify(self, ceremony: str, credential: dict) -> Optional[int]:
        from webauthn import verify_authentication_response
        from webauthn.helpers import base64url_to_bytes
        entry = self._take(ceremony)
        if not entry:
            return None
        try:
            raw_id = base64url_to_bytes(credential.get("rawId") or credential.get("id") or "")
        except Exception:
            return None
        row = self.store.one("SELECT * FROM passkeys WHERE credential_id=?", (raw_id,))
        if not row:
            return None
        try:
            result = verify_authentication_response(
                credential=credential, expected_challenge=entry[0], expected_rp_id=entry[2], expected_origin=entry[3],
                credential_public_key=row["public_key"], credential_current_sign_count=row["sign_count"])
        except Exception:
            return None
        self.store.execute("UPDATE passkeys SET sign_count=?, last_used=? WHERE id=?", (result.new_sign_count, time.time(), row["id"]))
        return row["user_id"]
