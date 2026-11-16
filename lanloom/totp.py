"""Time-based one-time passwords (RFC 6238 on top of RFC 4226) and recovery codes.

* TOTP: SHA-1, 6 digits, 30-second steps - what every authenticator app understands. A code is accepted for the current
  step and one step either side (clock drift); the caller remembers the last accepted step, so a code can be used once.
* Recovery codes: ten random codes of 60 bits each, shown once; only a keyed hash (HMAC-SHA-256 with a key derived from
  the instance key) is stored, so the database alone does not reveal them.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote

STEP = 30
DIGITS = 6
WINDOW = 1                 # steps either side
SECRET_BYTES = 20          # 160 bits, the size RFC 4226 recommends
RECOVERY_COUNT = 10
_RC_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o, 1/l/i


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _key(secret_b32: str) -> bytes:
    s = (secret_b32 or "").strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(secret_b32: str, counter: int) -> str:
    digest = hmac.new(_key(secret_b32), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def step_of(t: Optional[float] = None) -> int:
    return int((time.time() if t is None else t) // STEP)


def code_at(secret_b32: str, t: Optional[float] = None) -> str:
    return hotp(secret_b32, step_of(t))


def normalise_code(code) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())


def verify(secret_b32: str, code, last_step: int = 0, t: Optional[float] = None) -> Optional[int]:
    """The matched step when `code` is valid now (+-WINDOW) and newer than `last_step`, else None."""
    code = normalise_code(code)
    if len(code) != DIGITS or not secret_b32:
        return None
    now = step_of(t)
    found = None
    for step in range(now - WINDOW, now + WINDOW + 1):
        # compare every candidate (no early exit), constant time per comparison
        if hmac.compare_digest(hotp(secret_b32, step), code) and step > last_step:
            found = step
    return found


def otpauth_uri(secret_b32: str, account: str, issuer: str = "Lanloom") -> str:
    label = quote("%s:%s" % (issuer, account), safe="")
    return "otpauth://totp/%s?secret=%s&issuer=%s&algorithm=SHA1&digits=%d&period=%d" % (
        label, secret_b32, quote(issuer, safe=""), DIGITS, STEP)


# ---------------------------------------------------------------------------- recovery codes
def new_recovery_codes(n: int = RECOVERY_COUNT) -> list:
    out = []
    for _ in range(n):
        raw = "".join(secrets.choice(_RC_ALPHABET) for _ in range(12))     # 12 x log2(31) = 59.5 bits
        out.append("%s-%s-%s" % (raw[:4], raw[4:8], raw[8:]))
    return out


def normalise_recovery(code) -> str:
    return "".join(ch for ch in str(code or "").lower() if ch in _RC_ALPHABET)


def recovery_hash(pepper: bytes, user_id: int, code) -> str:
    return hmac.new(pepper, ("%d:%s" % (user_id, normalise_recovery(code))).encode("ascii"), hashlib.sha256).hexdigest()
