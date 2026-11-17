"""Web Push without an extra package: message encryption (RFC 8291, aes128gcm) and VAPID sign-in (RFC 8292).

The browser of a signed-in person subscribes with the public VAPID key of this Lanloom and hands back an endpoint at its
push service plus two keys. Lanloom encrypts each message for that one browser and signs a short-lived token with its
private VAPID key, which lives in the encrypted secret store and is never shown or logged. The push service only ever
sees ciphertext.
"""
import base64
import ipaddress
import json
import os
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TIMEOUT = 15
RECORD_SIZE = 4096
MAX_PAYLOAD = 3000
DEFAULT_SUBJECT = "https://github.com/robeertm/lanloom"   # identifier: project address as VAPID contact (no personal data)
ENDPOINT_RE = re.compile(r"\Ahttps://[^\s/?#]{1,253}(:\d{1,5})?/[^\s]{1,900}\Z")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = str(text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------- keys
def new_vapid() -> tuple:
    """(private key as PEM text, public key raw base64url)."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return pem, public_of(key)


def load_private(pem: str):
    return serialization.load_pem_private_key(pem.encode(), password=None)


def public_of(key) -> str:
    return b64u(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))


# ---------------------------------------------------------------- subscriptions
def clean_subscription(sub: dict) -> dict:
    """{endpoint, p256dh, auth} checked. Raises ValueError(reason key)."""
    if not isinstance(sub, dict):
        raise ValueError("notify.err.push_sub")
    endpoint = str(sub.get("endpoint") or "").strip()
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else sub
    if not ENDPOINT_RE.match(endpoint):
        raise ValueError("notify.err.push_sub")
    host = urllib.parse.urlsplit(endpoint).hostname or ""
    if host in ("localhost",) or host.endswith(".local") or "." not in host:
        raise ValueError("notify.err.push_sub")
    try:
        ipaddress.ip_address(host.strip("[]"))
        literal = True
    except ValueError:
        literal = False
    if literal:
        raise ValueError("notify.err.push_sub")       # push services have names; an address literal is not one
    try:
        p256dh, auth = unb64u(keys.get("p256dh")), unb64u(keys.get("auth"))
    except (ValueError, TypeError):
        raise ValueError("notify.err.push_sub")
    if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
        raise ValueError("notify.err.push_sub")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    except ValueError:
        raise ValueError("notify.err.push_sub")
    return {"endpoint": endpoint, "p256dh": b64u(p256dh), "auth": b64u(auth)}


# ---------------------------------------------------------------- RFC 8291 encryption
def _hmac(key: bytes, data: bytes) -> bytes:
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def encrypt(payload: bytes, p256dh: bytes, auth: bytes, salt: bytes = None, as_private=None) -> bytes:
    """aes128gcm body for one browser (single record). ``salt``/``as_private`` only for the test vector."""
    salt = salt or os.urandom(16)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = as_private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ua = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    shared = as_private.exchange(ec.ECDH(), ua)
    prk_key = _hmac(auth, shared)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + p256dh + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    if len(payload) > RECORD_SIZE - 17:
        raise ValueError("payload too large")
    cipher = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public + cipher


# ---------------------------------------------------------------- RFC 8292 VAPID
def vapid_header(endpoint: str, private_pem: str, subject: str = "", now: float = None) -> str:
    key = load_private(private_pem)
    parts = urllib.parse.urlsplit(endpoint)
    claims = {"aud": "%s://%s" % (parts.scheme, parts.netloc), "exp": int((now or time.time()) + 12 * 3600),
              "sub": subject if re.match(r"\A(mailto:[^\s]+|https://[^\s]+)\Z", subject or "") else DEFAULT_SUBJECT}
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    der = key.sign(("%s.%s" % (head, body)).encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return "vapid t=%s.%s.%s, k=%s" % (head, body, sig, public_of(key))


def send(sub: dict, message: dict, private_pem: str, subject: str = "", urgency: str = "normal", ttl: int = 86400,
         opener=None) -> tuple:
    """(ok, reason key). ``reason`` 'notify.push.gone' means the browser unsubscribed: drop the subscription. Never raises."""
    try:
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
        if len(payload) > MAX_PAYLOAD:
            message = dict(message, text=str(message.get("text") or "")[:600])
            payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()[:MAX_PAYLOAD]
        body = encrypt(payload, unb64u(sub["p256dh"]), unb64u(sub["auth"]))
        req = urllib.request.Request(sub["endpoint"], data=body, method="POST", headers={
            "Authorization": vapid_header(sub["endpoint"], private_pem, subject), "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream", "TTL": str(int(ttl)), "Urgency": urgency if urgency in ("low", "normal", "high") else "normal"})
        (opener or _open)(req)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return False, "notify.push.gone"
        if exc.code in (401, 403):
            return False, "report.send.auth"
        return False, "report.send.rejected"
    except (ssl.SSLError, ssl.CertificateError):
        return False, "report.send.tls"
    except (socket.timeout, TimeoutError):
        return False, "report.send.timeout"
    except (urllib.error.URLError, OSError):
        return False, "report.send.unreachable"
    except Exception:
        return False, "report.send.error"
    return True, "report.send.ok"


def _open(req) -> None:
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as resp:
        resp.read(2048)
