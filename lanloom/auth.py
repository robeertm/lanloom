"""Accounts, sessions, passkeys and request guards.

* Sign-in is mandatory; there is no open mode.
* Passwords: argon2id. Sessions: random 256-bit token in an HttpOnly, SameSite=Strict cookie; only its hash is stored.
* First run: the first account can only be created with the one-time setup code printed to the container log
  (and written to <data>/setup-code.txt). Whoever reaches the page first cannot take over the instance without it.
* State-changing requests need the header `X-Lanloom: 1` (forces a CORS preflight, so other sites cannot send it)
  and, when the browser sends an Origin header, an origin matching the Host header or LANLOOM_TRUSTED_ORIGINS.
* Sign-in attempts are rate limited per client address (IPv6 per /64). Behind a reverse proxy the forwarding
  headers count only when the direct peer is listed in LANLOOM_TRUSTED_PROXIES; by default only the peer counts.
* Passkeys (WebAuthn) are offered in secure contexts (HTTPS or localhost) in addition to the password.
* Roles (viewer / operator / admin, see access.py), second factor (TOTP with recovery codes, or a passkey), required
  per role after a grace period, step-up for critical actions, invitation and reset links, a session list per user,
  security notices for sign-ins from a new address or device and for lock-outs.
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from . import access, textfmt, totp
from .store import Store

SESSION_COOKIE = "ll_session"
SESSION_DAYS = 30
SESSION_IDLE_DAYS = 14
FOREIGN_HOURS = 8              # "public computer": short session, no persistent cookie
FOREIGN_IDLE_MIN = 30
STEPUP_MIN = 10                # a sign-in or a confirmation counts as recent for this long
MIN_PASSWORD = 10
USERNAME_MAX = 64
INVITE_HOURS = 24
TICKET_S = 300                 # password accepted, waiting for the second factor
TICKET_TRIES = 5
TOTP_PENDING_S = 900
GRACE_DEFAULT_H = 48
GRACE_CHOICES = (0, 24, 48, 72, 168, 336, 720)
POLICY_DEFAULT = {"mfa_required": {"viewer": False, "operator": False, "admin": True}, "mfa_grace_hours": GRACE_DEFAULT_H}

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


# ---------------------------------------------------------------------------- client address
log = logging.getLogger("lanloom.auth")


def parse_proxies(entries) -> tuple:
    """Trusted proxy addresses or networks (comma-separated text or a sequence); invalid entries are skipped."""
    if isinstance(entries, str):
        entries = entries.split(",")
    nets = []
    for raw in entries or ():
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            log.warning("LANLOOM_TRUSTED_PROXIES: ignoring %r (not an address or network)", raw)
    return tuple(nets)


def _ip(text: str):
    """An address from a header entry ("a.b.c.d", "a.b.c.d:port", "[v6]:port", "v6"), or None."""
    text = (text or "").strip()
    if text.startswith("[") and "]" in text:
        text = text[1:text.index("]")]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if ip.version == 6 else ip


def _trusted(ip, proxies) -> bool:
    return ip is not None and any(ip.version == net.version and ip in net for net in proxies)


def client_address(peer: str, headers, proxies) -> str:
    """The address a request counts as. X-Forwarded-For / X-Real-IP are believed only when the direct peer is a
    trusted proxy. X-Forwarded-For is read from the right: every proxy appends the address it saw, so the right-most
    entry that is not itself a trusted proxy is the client; entries left of it may be forged by the client."""
    peer_ip = _ip(peer)
    if not proxies or not _trusted(peer_ip, proxies):
        return peer
    chain = [part for value in headers.getlist("x-forwarded-for") for part in value.split(",") if part.strip()]
    if chain:
        nearest = peer_ip
        for entry in reversed(chain):
            ip = _ip(entry)
            if ip is None:
                break               # garbage from the client side: stay with the last trusted hop
            if not _trusted(ip, proxies):
                return str(ip)
            nearest = ip
        return str(nearest)
    real = _ip(headers.get("x-real-ip", ""))
    return str(real) if real is not None else peer


def peer_host(addr) -> str:
    """Host part of an ASGI address (scope["client"] / scope["server"], or the framework's request.client).

    Over TCP it is (host, port). Over a unix socket the server reports no peer address: newer uvicorn gives the
    client as None and the server as (socket path, None), other servers ("", 0) or (path, None). A socket path is not
    an address, so it comes back as "" - it never matches a trusted proxy, never counts as loopback."""
    if not addr:
        return ""
    try:
        host, port = addr[0], addr[1]
    except (TypeError, IndexError, KeyError):
        return ""
    host = str(host or "")
    if port is None or "/" in host:
        return ""
    return host


def from_trusted_proxy(peer: str, proxies) -> bool:
    return bool(proxies) and _trusted(_ip(peer), proxies)


def limit_key(address: str) -> str:
    """Rate-limit bucket: IPv4 per address, IPv6 per /64 (one connection usually owns a whole /64)."""
    ip = _ip(address)
    if ip is None:
        return address
    if ip.version == 6:
        return str(ipaddress.ip_network((ip, 64), strict=False))
    return str(ip)


# ---------------------------------------------------------------------------- rate limit
class LoginLimiter:
    """Per-client failure counter with growing lock-out (5 failures -> 60 s, doubling, max 1 h).

    Memory is bounded: when full, entries without an active lock-out go first (oldest first), so flooding the
    limiter with fresh addresses cannot reset the lock-out of an attacker."""

    def __init__(self, max_entries: int = 10000):
        self._lock = threading.Lock()
        self._state = {}   # client -> [failures, locked_until, last_failure]
        self.max_entries = max_entries

    def wait_seconds(self, client: str) -> float:
        with self._lock:
            entry = self._state.get(client)
            if not entry:
                return 0.0
            return max(0.0, entry[1] - time.time())

    def failure(self, client: str) -> None:
        with self._lock:
            now = time.time()
            entry = self._state.setdefault(client, [0, 0.0, now])
            entry[0] += 1
            entry[2] = now
            if entry[0] >= 5:
                entry[1] = now + min(3600, 60 * 2 ** (entry[0] - 5))
            if len(self._state) > self.max_entries:
                self._prune(now, keep=client)

    def _prune(self, now: float, keep: str) -> None:
        # down to 90 % so the sort does not run on every failure once full
        target = int(self.max_entries * 0.9)
        order = sorted((k for k in self._state if k != keep), key=lambda k: (self._state[k][1] > now, self._state[k][2]))
        for k in order[:max(0, len(self._state) - target)]:
            del self._state[k]

    def success(self, client: str) -> None:
        with self._lock:
            self._state.pop(client, None)


# ---------------------------------------------------------------------------- accounts
USER_COLS = "id, username, user_handle, created, role, disabled, source, mfa_due, last_login, prefs, sites"


class Accounts:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def validate(username: str, password: str) -> Optional[str]:
        problem = Accounts.validate_name(username)
        if problem:
            return problem
        if len(password or "") < MIN_PASSWORD:
            return "auth.err.password_short"
        return None

    @staticmethod
    def validate_name(username: str) -> Optional[str]:
        username = (username or "").strip()
        if not username or len(username) > USERNAME_MAX or any(c.isspace() or ord(c) < 32 for c in username):
            return "auth.err.username"
        return None

    def create(self, username: str, password: Optional[str], role: str = access.DEFAULT_ROLE, source: str = "local",
               mfa_due: Optional[float] = None) -> int:
        if role not in access.ROLES:
            raise ValueError("users.err.role")
        pw_hash = _hasher.hash(password) if password else "!" + source        # "!..." never verifies
        cur = self.store.execute(
            "INSERT INTO users(username, pw_hash, user_handle, created, role, source, mfa_due) VALUES(?,?,?,?,?,?,?)",
            (username.strip(), pw_hash, secrets.token_bytes(32), time.time(), role, source, mfa_due),
        )
        return cur.lastrowid

    def exists(self, username: str) -> bool:
        return self.store.one("SELECT 1 AS x FROM users WHERE username=?", ((username or "").strip(),)) is not None

    def verify(self, username: str, password: str) -> Optional[dict]:
        row = self.store.one("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
        if not row or row["pw_hash"].startswith("!"):
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
        return self.store.one("SELECT %s FROM users WHERE id=?" % USER_COLS, (user_id,))

    def by_name(self, username: str) -> Optional[dict]:
        return self.store.one("SELECT %s FROM users WHERE username=?" % USER_COLS, ((username or "").strip(),))

    def all(self) -> list:
        return self.store.query("SELECT %s FROM users ORDER BY username COLLATE NOCASE" % USER_COLS)

    def enabled_admins(self, excluding: Optional[int] = None) -> int:
        return self.store.one("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0 AND id IS NOT ?",
                              (excluding,))["n"]

    def set_role(self, user_id: int, role: str, mfa_due: Optional[float] = None) -> None:
        if role not in access.ROLES:
            raise ValueError("users.err.role")
        self.store.execute("UPDATE users SET role=?, mfa_due=? WHERE id=?", (role, mfa_due, user_id))

    def set_disabled(self, user_id: int, disabled: bool) -> None:
        self.store.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, user_id))
        if disabled:
            self.store.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def delete(self, user_id: int) -> bool:
        return self.store.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount > 0

    def touch_login(self, user_id: int) -> None:
        self.store.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), user_id))

    def prefs(self, user_id: int) -> dict:
        row = self.store.one("SELECT prefs FROM users WHERE id=?", (user_id,))
        try:
            data = json.loads(row["prefs"]) if row else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def set_prefs(self, user_id: int, patch: dict) -> dict:
        data = self.prefs(user_id)
        if "language" in patch and (patch["language"] is None or patch["language"] in textfmt.languages()):
            data["language"] = patch["language"]
        seen = patch.get("whatsnew_seen")
        if isinstance(seen, str) and re.fullmatch(r"\d{1,6}\.\d{1,6}\.\d{1,6}", seen):
            data["whatsnew_seen"] = seen      # newest version this person looked at in "What's new?"
        self.store.execute("UPDATE users SET prefs=? WHERE id=?", (json.dumps(data, separators=(",", ":")), user_id))
        return data


_DUMMY_HASH = _hasher.hash("lanloom-timing-equaliser")

# A hash written by an older version of the password library (argon2-cffi 23.1, its defaults) for a sample password,
# the same one tests/fixtures/auth_old_libs.json holds. Stored hashes of real accounts look like this.
_OLD_HASH_SAMPLE = ("sample-password-before-update",
                    "$argon2id$v=19$m=65536,t=3,p=4$EBBrWM5BnqaOKqsSLYOZOQ$UoemQLyuCDrIguDTsEwQpMURCkcG0NZ7bGiIcjAuTTE")


def password_hashes_compatible() -> bool:
    """Self-test: a hash stored by an older library version still verifies, a wrong password does not, and the
    current parameters do not ask for a rehash (an update must not rewrite every stored hash on the next sign-in)."""
    sample, stored = _OLD_HASH_SAMPLE
    try:
        _hasher.verify(stored, sample)
    except (VerificationError, InvalidHashError):
        return False
    try:
        _hasher.verify(stored, sample + "!")
        return False
    except VerifyMismatchError:
        pass
    except (VerificationError, InvalidHashError):
        return False
    return not _hasher.check_needs_rehash(stored)


# ---------------------------------------------------------------------------- sessions
class Sessions:
    def __init__(self, store: Store, clock=time.time):
        self.store = store
        self.clock = clock

    def create(self, user_id: int, ip: str = "", agent: str = "", foreign: bool = False, stage: str = "full",
               method: str = "password") -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        life = FOREIGN_HOURS * 3600 if foreign else SESSION_DAYS * 86400
        self.store.execute(
            "INSERT INTO sessions(token_hash, user_id, created, last_seen, expires, sid, ip, agent, foreign_pc, stage, method, stepup_until)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (_hash_token(token), user_id, now, now, now + life, secrets.token_hex(12), str(ip)[:64], str(agent)[:80],
             1 if foreign else 0, stage, method, now + STEPUP_MIN * 60),
        )
        self.store.execute("DELETE FROM sessions WHERE expires < ? OR last_seen < ? OR (foreign_pc=1 AND last_seen < ?)",
                           (now, now - SESSION_IDLE_DAYS * 86400, now - FOREIGN_IDLE_MIN * 60))
        return token

    def resolve(self, token: Optional[str]) -> Optional[dict]:
        if not token or len(token) > 128:
            return None
        row = self.store.one(
            "SELECT s.token_hash, s.user_id, s.expires, s.last_seen, s.sid, s.stage, s.method, s.stepup_until, s.foreign_pc,"
            " u.username, u.role, u.disabled, u.source FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (_hash_token(token),),
        )
        now = self.clock()
        if not row or row["expires"] < now or row["last_seen"] < now - SESSION_IDLE_DAYS * 86400 or row["disabled"]:
            return None
        if row["foreign_pc"] and row["last_seen"] < now - FOREIGN_IDLE_MIN * 60:
            return None
        if now - row["last_seen"] > (60 if row["foreign_pc"] else 300):
            self.store.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, row["token_hash"]))
        return {"id": row["user_id"], "username": row["username"], "role": row["role"], "sid": row["sid"], "stage": row["stage"],
                "method": row["method"], "stepup_until": row["stepup_until"], "foreign": bool(row["foreign_pc"]),
                "source": row["source"]}

    def destroy(self, token: Optional[str]) -> None:
        if token:
            self.store.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))

    def list(self, user_id: int) -> list:
        now = self.clock()
        return self.store.query(
            "SELECT sid, created, last_seen, expires, ip, agent, foreign_pc AS foreign_pc, stage, method FROM sessions"
            " WHERE user_id=? AND expires >= ? ORDER BY last_seen DESC", (user_id, now))

    def destroy_sid(self, user_id: int, sid: str) -> bool:
        return self.store.execute("DELETE FROM sessions WHERE user_id=? AND sid=?", (user_id, str(sid)[:40])).rowcount > 0

    def destroy_all(self, user_id: int) -> int:
        return self.store.execute("DELETE FROM sessions WHERE user_id=?", (user_id,)).rowcount

    def mark_stepup(self, sid: str) -> float:
        until = self.clock() + STEPUP_MIN * 60
        self.store.execute("UPDATE sessions SET stepup_until=? WHERE sid=?", (until, sid))
        return until

    def promote(self, sid: str, method: str) -> None:
        """An enrolment session becomes a full one once the second factor is set up."""
        self.store.execute("UPDATE sessions SET stage='full', method=?, stepup_until=? WHERE sid=?",
                           (method, self.clock() + STEPUP_MIN * 60, sid))

    def fresh(self, user: dict) -> bool:
        return float(user.get("stepup_until") or 0) > self.clock()


# ---------------------------------------------------------------------------- second factor
class SecondFactor:
    """TOTP secrets live encrypted in the vault ("totp:<user>"); recovery codes only as keyed hashes."""

    def __init__(self, store: Store, vault, pepper: bytes, clock=time.time):
        self.store = store
        self.vault = vault
        self.pepper = pepper
        self.clock = clock

    def has_totp(self, user_id: int) -> bool:
        return self.vault.has("totp:%d" % user_id)

    def passkey_count(self, user_id: int) -> int:
        return self.store.one("SELECT COUNT(*) AS n FROM passkeys WHERE user_id=?", (user_id,))["n"]

    def recovery_left(self, user_id: int) -> int:
        return self.store.one("SELECT COUNT(*) AS n FROM recovery_codes WHERE user_id=? AND used IS NULL", (user_id,))["n"]

    def summary(self, user_id: int) -> dict:
        t, p = self.has_totp(user_id), self.passkey_count(user_id)
        return {"totp": t, "passkeys": p, "recovery_left": self.recovery_left(user_id), "any": bool(t or p)}

    def has_any(self, user_id: int) -> bool:
        return self.has_totp(user_id) or self.passkey_count(user_id) > 0

    def begin(self, user_id: int) -> str:
        secret = totp.new_secret()
        self.vault.put("totp_pending:%d" % user_id, json.dumps({"s": secret, "t": self.clock()}))
        return secret

    def enable(self, user_id: int, code) -> Optional[list]:
        raw = self.vault.get("totp_pending:%d" % user_id)
        try:
            pending = json.loads(raw) if raw else None
        except ValueError:
            pending = None
        if not pending or self.clock() - float(pending.get("t", 0)) > TOTP_PENDING_S:
            return None
        step = totp.verify(pending["s"], code, 0, self.clock())
        if step is None:
            return None
        self.vault.put("totp:%d" % user_id, pending["s"])
        self.vault.delete("totp_pending:%d" % user_id)
        self.store.execute("UPDATE users SET totp_step=? WHERE id=?", (step, user_id))
        return self.new_recovery(user_id)

    def disable(self, user_id: int) -> None:
        self.vault.delete("totp:%d" % user_id)
        self.vault.delete("totp_pending:%d" % user_id)
        if not self.passkey_count(user_id):
            self.store.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))

    def clear_all(self, user_id: int) -> None:
        """Admin reset: no second factor left (passkeys, one-time codes, recovery codes)."""
        self.vault.delete("totp:%d" % user_id)
        self.vault.delete("totp_pending:%d" % user_id)
        self.store.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
        self.store.execute("DELETE FROM passkeys WHERE user_id=?", (user_id,))

    def check_totp(self, user_id: int, code) -> bool:
        secret = self.vault.get("totp:%d" % user_id)
        if not secret:
            return False
        row = self.store.one("SELECT totp_step FROM users WHERE id=?", (user_id,))
        step = totp.verify(secret, code, int(row["totp_step"] or 0) if row else 0, self.clock())
        if step is None:
            return False
        # the conditional update makes two concurrent uses of the same code fail for one of them
        return self.store.execute("UPDATE users SET totp_step=? WHERE id=? AND totp_step < ?", (step, user_id, step)).rowcount == 1

    def new_recovery(self, user_id: int) -> list:
        codes = totp.new_recovery_codes()
        with self.store.transaction() as tx:
            tx.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
            for c in codes:
                tx.execute("INSERT INTO recovery_codes(user_id, code_hash) VALUES(?,?)", (user_id, totp.recovery_hash(self.pepper, user_id, c)))
        return codes

    def check_recovery(self, user_id: int, code) -> bool:
        if len(totp.normalise_recovery(code)) != 12:
            return False
        h = totp.recovery_hash(self.pepper, user_id, code)
        return self.store.execute("UPDATE recovery_codes SET used=? WHERE user_id=? AND code_hash=? AND used IS NULL",
                                  (self.clock(), user_id, h)).rowcount == 1

    def check(self, user_id: int, kind: str, code) -> bool:
        if kind == "totp":
            return self.check_totp(user_id, code)
        if kind == "recovery":
            return self.check_recovery(user_id, code)
        return False


# ---------------------------------------------------------------------------- pending sign-ins (password ok, second factor missing)
class Tickets:
    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._items = {}       # ticket -> {"user", "client", "foreign", "t", "tries"}
        self.clock = clock

    def create(self, user_id: int, client: str, foreign: bool) -> str:
        ticket = secrets.token_urlsafe(24)
        now = self.clock()
        with self._lock:
            for k in [k for k, v in self._items.items() if now - v["t"] > TICKET_S]:
                del self._items[k]
            if len(self._items) > 5000:
                self._items.pop(next(iter(self._items)))
            self._items[ticket] = {"user": user_id, "client": client, "foreign": foreign, "t": now, "tries": 0}
        return ticket

    def get(self, ticket: str, client: str) -> Optional[dict]:
        with self._lock:
            entry = self._items.get(str(ticket or ""))
            if not entry or self.clock() - entry["t"] > TICKET_S or entry["client"] != client:
                return None
            return dict(entry)

    def failed(self, ticket: str) -> int:
        with self._lock:
            entry = self._items.get(ticket)
            if not entry:
                return TICKET_TRIES
            entry["tries"] += 1
            if entry["tries"] >= TICKET_TRIES:
                del self._items[ticket]
                return TICKET_TRIES
            return entry["tries"]

    def drop(self, ticket: str) -> None:
        with self._lock:
            self._items.pop(ticket, None)


# ---------------------------------------------------------------------------- invitations and reset links
class Invites:
    def __init__(self, store: Store, clock=time.time):
        self.store = store
        self.clock = clock

    def create(self, kind: str, role: str, created_by: str, user_id: Optional[int] = None, clear_2fa: bool = False,
               note: str = "", sites: str = "*") -> tuple:
        if kind not in ("invite", "reset") or role not in access.ROLES:
            raise ValueError("users.err.role")
        token = secrets.token_urlsafe(32)
        now = self.clock()
        if kind == "reset":       # one open reset link per user
            self.store.execute("DELETE FROM invites WHERE kind='reset' AND user_id=? AND used IS NULL", (user_id,))
        cur = self.store.execute(
            "INSERT INTO invites(token_hash, kind, role, user_id, clear_2fa, note, created, expires, created_by, sites)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (_hash_token(token), kind, role, user_id, 1 if clear_2fa else 0, str(note or "")[:80], now, now + INVITE_HOURS * 3600,
             str(created_by)[:64], sites if isinstance(sites, str) else "*"))
        return cur.lastrowid, token

    def check(self, token: str) -> Optional[dict]:
        if not token or len(str(token)) > 128:
            return None
        row = self.store.one("SELECT * FROM invites WHERE token_hash=?", (_hash_token(str(token)),))
        if not row or row["used"] or row["expires"] < self.clock():
            return None
        return row

    def consume(self, invite_id: int) -> bool:
        return self.store.execute("UPDATE invites SET used=? WHERE id=? AND used IS NULL", (self.clock(), invite_id)).rowcount == 1

    def open(self) -> list:
        now = self.clock()
        self.store.execute("DELETE FROM invites WHERE expires < ? - 86400 * 30", (now,))
        return self.store.query(
            "SELECT i.id, i.kind, i.role, i.note, i.created, i.expires, i.created_by, i.used, u.username FROM invites i"
            " LEFT JOIN users u ON u.id=i.user_id WHERE i.used IS NULL AND i.expires >= ? ORDER BY i.created DESC", (now,))

    def revoke(self, invite_id: int) -> bool:
        return self.store.execute("DELETE FROM invites WHERE id=? AND used IS NULL", (invite_id,)).rowcount > 0


# ---------------------------------------------------------------------------- security notices
class Notices:
    """Notes in the app: sign-in from a new address or device, lock-outs, recovery codes used, password reset."""

    def __init__(self, store: Store, clock=time.time):
        self.store = store
        self.clock = clock
        self.listener = None          # (user_id, kind, data): the notification rules may pass it on (event security_notice)

    def add(self, user_id: int, kind: str, data: Optional[dict] = None) -> None:
        self.store.execute("INSERT INTO notices(user_id, t, kind, data) VALUES(?,?,?,?)",
                           (user_id, self.clock(), kind, json.dumps(data or {}, separators=(",", ":"))))
        if self.listener is not None:
            try:
                self.listener(user_id, kind, dict(data or {}))
            except Exception:
                pass
        # bounded: the newest 200 per user
        self.store.execute("DELETE FROM notices WHERE user_id=? AND id NOT IN (SELECT id FROM notices WHERE user_id=? ORDER BY id DESC LIMIT 200)",
                           (user_id, user_id))

    def unseen(self, user_id: int) -> list:
        rows = self.store.query("SELECT id, t, kind, data FROM notices WHERE user_id=? AND seen IS NULL ORDER BY id DESC LIMIT 20", (user_id,))
        for r in rows:
            try:
                r["data"] = json.loads(r["data"])
            except ValueError:
                r["data"] = {}
        return rows

    def seen(self, user_id: int, ids=None) -> int:
        if ids:
            ids = [int(i) for i in ids if isinstance(i, int)][:100]
            if not ids:
                return 0
            return self.store.execute("UPDATE notices SET seen=? WHERE user_id=? AND seen IS NULL AND id IN (%s)" % ",".join("?" * len(ids)),
                                      (self.clock(), user_id, *ids)).rowcount
        return self.store.execute("UPDATE notices SET seen=? WHERE user_id=? AND seen IS NULL", (self.clock(), user_id)).rowcount

    def known(self, user_id: int, address_key: str, device_key: str) -> list:
        """Remember the address and device of a sign-in; the kinds that were new ([] on the very first sign-in)."""
        now = self.clock()
        first_ever = self.store.one("SELECT 1 AS x FROM known_logins WHERE user_id=? LIMIT 1", (user_id,)) is None
        new = []
        for kind, value in (("addr", address_key), ("dev", device_key)):
            cur = self.store.execute("UPDATE known_logins SET last=? WHERE user_id=? AND kind=? AND value=?", (now, user_id, kind, value))
            if cur.rowcount == 0:
                self.store.execute("INSERT INTO known_logins(user_id, kind, value, first, last) VALUES(?,?,?,?,?)",
                                   (user_id, kind, value, now, now))
                new.append(kind)
        # forget what was not seen for a year, bounded per user
        self.store.execute("DELETE FROM known_logins WHERE user_id=? AND last < ?", (user_id, now - 365 * 86400))
        return [] if first_ever else new


_UA_SCRIPT = re.compile(r"curl/|wget/|python-|go-http|okhttp|java/", re.I)   # identifier: user-agent tokens
_UA_TABLET = re.compile(r"Tablet")        # identifier: user-agent token
_UA_PHONE = re.compile(r"Mobi")           # identifier: user-agent token (mobile browsers say "Mobile")


def agent_label(ua: str) -> str:
    """Kind of device, never a product name: "computer", "phone", "tablet", "script" or "unknown" (the page translates it).
    Telling two sessions apart works by address and time; recognising a device again uses device_key()."""
    ua = ua or ""
    if not ua.strip():
        return "unknown"
    if _UA_SCRIPT.search(ua):
        return "script"
    if _UA_TABLET.search(ua):
        return "tablet"
    if _UA_PHONE.search(ua):
        return "phone"
    return "computer"


def device_key(ua: str) -> str:
    return hashlib.sha256(("lanloom-device:" + (ua or "")).encode("utf-8", "replace")).hexdigest()[:24]


# ---------------------------------------------------------------------------- security policy
class Policy:
    def __init__(self, store: Store):
        self.store = store

    def get(self) -> dict:
        raw = self.store.get_setting("security", None)
        out = json.loads(json.dumps(POLICY_DEFAULT))
        if isinstance(raw, dict):
            req = raw.get("mfa_required") if isinstance(raw.get("mfa_required"), dict) else {}
            for role in access.ROLES:
                if isinstance(req.get(role), bool):
                    out["mfa_required"][role] = req[role]
            if raw.get("mfa_grace_hours") in GRACE_CHOICES:
                out["mfa_grace_hours"] = raw["mfa_grace_hours"]
        return out

    def set(self, patch: dict) -> dict:
        cur = self.get()
        req = patch.get("mfa_required")
        if isinstance(req, dict):
            for role in access.ROLES:
                if isinstance(req.get(role), bool):
                    cur["mfa_required"][role] = req[role]
        if "mfa_grace_hours" in patch:
            if patch["mfa_grace_hours"] not in GRACE_CHOICES:
                raise ValueError("security.err.grace")
            cur["mfa_grace_hours"] = patch["mfa_grace_hours"]
        self.store.set_setting("security", cur)
        return cur

    def required(self, role: str) -> bool:
        return bool(self.get()["mfa_required"].get(role))

    def due_from_now(self) -> float:
        return time.time() + self.get()["mfa_grace_hours"] * 3600


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
    MAX_PENDING = 2000   # sign-in options need no session: a flood must not grow memory without bound

    def __init__(self, store: Store):
        self.store = store
        self._pending = {}   # ceremony id -> (challenge, user_id|None, rp_id, origin, created), oldest first
        self._lock = threading.Lock()

    def _remember(self, challenge: bytes, user_id: Optional[int], rp_id: str, origin: str) -> str:
        cid = secrets.token_urlsafe(16)
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._pending.items() if now - v[4] > self.TTL]:
                self._pending.pop(k, None)
            while len(self._pending) >= self.MAX_PENDING:          # the oldest open ceremony goes first
                self._pending.pop(next(iter(self._pending)))
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

    def login_options(self, rp_id: str, origin: str, user_id: Optional[int] = None) -> dict:
        """Sign-in challenge; with `user_id` (step-up) only that user's passkeys are offered and accepted."""
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement
        allow = None
        if user_id is not None:
            allow = [PublicKeyCredentialDescriptor(id=r["credential_id"])
                     for r in self.store.query("SELECT credential_id FROM passkeys WHERE user_id=?", (user_id,))]
        opts = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.PREFERRED, timeout=120000,
                                               allow_credentials=allow)
        cid = self._remember(opts.challenge, user_id, rp_id, origin)
        return {"ceremony": cid, "options": json.loads(options_to_json(opts))}

    def login_verify(self, ceremony: str, credential: dict, user_id: Optional[int] = None) -> Optional[int]:
        from webauthn import verify_authentication_response
        from webauthn.helpers import base64url_to_bytes
        entry = self._take(ceremony)
        if not entry or entry[1] != user_id:       # a step-up ceremony cannot be used to sign in, nor the other way round
            return None
        try:
            raw_id = base64url_to_bytes(credential.get("rawId") or credential.get("id") or "")
        except Exception:
            return None
        row = self.store.one("SELECT * FROM passkeys WHERE credential_id=?", (raw_id,))
        if not row or (user_id is not None and row["user_id"] != user_id):
            return None
        try:
            result = verify_authentication_response(
                credential=credential, expected_challenge=entry[0], expected_rp_id=entry[2], expected_origin=entry[3],
                credential_public_key=row["public_key"], credential_current_sign_count=row["sign_count"])
        except Exception:
            return None
        self.store.execute("UPDATE passkeys SET sign_count=?, last_used=? WHERE id=?", (result.new_sign_count, time.time(), row["id"]))
        return row["user_id"]


# ---------------------------------------------------------------------------- trusted auth proxy
class ProxyAuth:
    """Sign-in by a header set by an authenticating reverse proxy (off unless LANLOOM_AUTH_HEADER is set).

    The header is believed only when the direct peer is listed in LANLOOM_TRUSTED_PROXIES; from anybody else it is
    ignored. The role comes from the groups header through LANLOOM_AUTH_GROUP_ROLES (highest match wins), otherwise
    from LANLOOM_AUTH_DEFAULT_ROLE; without either the user is refused. A local account with the same name is the same
    person (the proxy is trusted by configuration); its role follows the groups when a group matches."""

    def __init__(self, cfg, proxies, app_state):
        self.header = (getattr(cfg, "auth_header", "") or "").lower()
        self.groups_header = (getattr(cfg, "auth_groups_header", "") or "").lower()
        self.group_roles = tuple(getattr(cfg, "auth_group_roles", ()) or ())
        self.default_role = getattr(cfg, "auth_default_role", "") or ""
        self.proxies = proxies
        self.app = app_state
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.header and self.proxies)

    def role_for(self, groups: list) -> str:
        best = ""
        for group, role in self.group_roles:
            if group in groups and access.rank(role) > access.rank(best):
                best = role
        return best

    def user(self, peer: str, headers, agent: str = "", ip: str = ""):
        """None = not a proxy sign-in (fall back to the session); {} = refused; else the user."""
        if not self.enabled or not from_trusted_proxy(peer, self.proxies):
            return None
        name = (headers.get(self.header) or "").strip()
        if not name:
            return None
        if Accounts.validate_name(name):
            return {}
        groups = []
        if self.groups_header:
            raw = headers.get(self.groups_header) or ""
            groups = [g.strip() for g in raw.replace(";", ",").replace(" ", ",").split(",") if g.strip()]
        mapped = self.role_for(groups)
        with self._lock:
            row = self.app.accounts.by_name(name)
            if row is None:
                role = mapped or self.default_role
                if not role:
                    return {}
                uid = self.app.accounts.create(name, None, role=role, source="proxy")
                row = self.app.accounts.get(uid)
                self.app.audit.record("users.joined", row, target=name, ip=ip, agent=agent, detail={"via": "proxy", "role": role})
            elif mapped and row["role"] != mapped:
                self.app.accounts.set_role(row["id"], mapped, row["mfa_due"])
                self.app.audit.record("users.change", {"username": "proxy"}, target=name, ip=ip, agent=agent,
                                      detail={"role": mapped, "via": "proxy groups"})
                row = self.app.accounts.get(row["id"])
        if row["disabled"]:
            return {}
        return {"id": row["id"], "username": row["username"], "role": row["role"], "sid": None, "stage": "full",
                "method": "proxy", "stepup_until": float("inf"), "foreign": False, "source": "proxy"}
