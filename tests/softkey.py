"""A software passkey for tests: a P-256 key that answers WebAuthn registration and sign-in ceremonies the way a
platform authenticator does (attestation "none", user present + verified, counter per signature).

The key is derived from a label, so a passkey stored years ago (tests/fixtures/auth_old_libs.json) can sign in again
in a later test run without any key material in the repository. Test use only."""
import base64
import hashlib
import json
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

ORIGIN = "http://localhost:7380"
RP_ID = "localhost"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftKey:
    def __init__(self, label: str, counter: int = 0):
        scalar = int.from_bytes(hashlib.sha256(("lanloom soft passkey " + label).encode()).digest(), "big")
        self.key = ec.derive_private_key(scalar % (2 ** 255) + 1, ec.SECP256R1())
        self.credential_id = hashlib.sha256(("lanloom soft credential " + label).encode()).digest()[:20]
        self.counter = counter

    def cose_public(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False},
                          separators=(",", ":")).encode()

    def register(self, options: dict, origin: str = ORIGIN) -> dict:
        """Answer to navigator.credentials.create() for the JSON options Lanloom hands to the browser."""
        rp_hash = hashlib.sha256(options["rp"]["id"].encode()).digest()
        auth_data = (rp_hash + bytes([0x45]) + struct.pack(">I", self.counter) + bytes(16)
                     + struct.pack(">H", len(self.credential_id)) + self.credential_id + self.cose_public())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cdj = self._client_data("webauthn.create", options["challenge"], origin)
        return {"id": b64u(self.credential_id), "rawId": b64u(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "attestationObject": b64u(att), "transports": ["internal"]},
                "clientExtensionResults": {}, "authenticatorAttachment": "platform"}

    def sign_in(self, options: dict, origin: str = ORIGIN, user_handle: bytes = b"") -> dict:
        """Answer to navigator.credentials.get() for the JSON options Lanloom hands to the browser."""
        self.counter += 1
        rp_hash = hashlib.sha256(options["rpId"].encode()).digest()
        auth_data = rp_hash + bytes([0x05]) + struct.pack(">I", self.counter)
        cdj = self._client_data("webauthn.get", options["challenge"], origin)
        sig = self.key.sign(auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        resp = {"clientDataJSON": b64u(cdj), "authenticatorData": b64u(auth_data), "signature": b64u(sig)}
        if user_handle:
            resp["userHandle"] = b64u(user_handle)
        return {"id": b64u(self.credential_id), "rawId": b64u(self.credential_id), "type": "public-key",
                "response": resp, "clientExtensionResults": {}, "authenticatorAttachment": "platform"}
