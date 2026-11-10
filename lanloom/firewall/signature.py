"""Passkey (WebAuthn) signatures over a plan hash.

The browser asks the authenticator for an assertion whose challenge is the 32-byte SHA-256 of the canonical plan.
Both the web app (as a courtesy pre-check) and the executor (the check that counts) verify it:

* clientDataJSON: type ``webauthn.get``, challenge == plan hash, origin in the allowed list, not cross-origin
* authenticatorData: rpIdHash == SHA-256(rp id of the key), user present and user verified flags set,
  signature counter rising when the authenticator counts
* ES256 signature over authenticatorData || SHA-256(clientDataJSON) with the pinned public key

The executor knows public keys only from its own file, put there by the operator. A compromised web app therefore
cannot approve a plan on its own.
"""
import base64
import hashlib
import json


class Invalid(ValueError):
    pass


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s) -> bytes:
    s = str(s or "")
    if len(s) > 8192:
        raise Invalid("too long")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError):
        raise Invalid("base64url")


def cbor(b: bytes, i: int = 0, depth: int = 0):
    """Minimal CBOR reader (definite lengths only), enough for COSE keys and attestation objects."""
    if depth > 8 or i >= len(b):
        raise Invalid("cbor")
    ib = b[i]
    mt, ai = ib >> 5, ib & 31
    i += 1
    if ai < 24:
        val = ai
    elif ai in (24, 25, 26, 27):
        n = 1 << (ai - 24)
        val = int.from_bytes(b[i:i + n], "big")
        i += n
    else:
        raise Invalid("cbor length")
    if mt == 0:
        return val, i
    if mt == 1:
        return -1 - val, i
    if mt in (2, 3):
        d = b[i:i + val]
        if len(d) != val:
            raise Invalid("cbor short")
        return (d if mt == 2 else d.decode("utf-8")), i + val
    if mt == 4:
        out = []
        for _ in range(val):
            v, i = cbor(b, i, depth + 1)
            out.append(v)
        return out, i
    if mt == 5:
        out = {}
        for _ in range(val):
            k, i = cbor(b, i, depth + 1)
            v, i = cbor(b, i, depth + 1)
            out[k] = v
        return out, i
    if mt == 7:
        return {20: False, 21: True, 22: None}.get(val), i
    raise Invalid("cbor type")


def cose_to_xy(cose_bytes: bytes) -> tuple:
    """COSE_Key (EC2, P-256, ES256) -> (x hex, y hex)."""
    key, _ = cbor(cose_bytes)
    if not isinstance(key, dict) or key.get(1) != 2 or key.get(3) != -7 or key.get(-1) != 1:
        raise Invalid("only ES256 keys on P-256 can sign plans")
    x, y = key.get(-2), key.get(-3)
    if not isinstance(x, bytes) or not isinstance(y, bytes) or len(x) != 32 or len(y) != 32:
        raise Invalid("key coordinates")
    return x.hex(), y.hex()


def key_fingerprint(credential_id: bytes) -> str:
    return hashlib.sha256(credential_id).hexdigest()[:16]


def _verify_es256(x_hex: str, y_hex: str, message: bytes, der_sig: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        pub = ec.EllipticCurvePublicNumbers(int(x_hex, 16), int(y_hex, 16), ec.SECP256R1()).public_key()
        pub.verify(der_sig, message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_assertion(assertion: dict, challenge: bytes, keys: list, origins, counters: dict = None) -> tuple:
    """assertion = {credential_id, authenticator_data, client_data, signature} (base64url).
    keys = [{id, rp_id, x, y}]. -> (key id, new counter). Raises Invalid with a short reason."""
    if not isinstance(assertion, dict):
        raise Invalid("no signature")
    cid = str(assertion.get("credential_id") or "")
    key = next((k for k in keys if k.get("id") == cid), None)
    if not key:
        raise Invalid("unknown key")
    raw_cd = b64u_dec(assertion.get("client_data"))
    try:
        cd = json.loads(raw_cd)
    except ValueError:
        raise Invalid("client data")
    if not isinstance(cd, dict) or cd.get("type") != "webauthn.get":
        raise Invalid("wrong ceremony")
    if b64u_dec(cd.get("challenge")) != challenge:
        raise Invalid("signature belongs to another plan")
    if cd.get("origin") not in set(origins or ()) or cd.get("crossOrigin"):
        raise Invalid("foreign origin")
    ad = b64u_dec(assertion.get("authenticator_data"))
    if len(ad) < 37:
        raise Invalid("authenticator data")
    if ad[:32] != hashlib.sha256(str(key.get("rp_id") or "").encode()).digest():
        raise Invalid("rp id")
    flags = ad[32]
    if not (flags & 1) or not (flags & 4):
        raise Invalid("user not verified")
    if not _verify_es256(key["x"], key["y"], ad + hashlib.sha256(raw_cd).digest(), b64u_dec(assertion.get("signature"))):
        raise Invalid("bad signature")
    count = int.from_bytes(ad[33:37], "big")
    before = (counters or {}).get(cid)
    if before is not None and (count or before) and count <= before:
        raise Invalid("counter did not rise (cloned key?)")
    return cid, count
