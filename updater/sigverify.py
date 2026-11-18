"""Signature check of an update candidate, before anything is swapped.

Every published image index is signed keyless by the project's publishing workflow (Sigstore: a short-lived
certificate for the workflow identity, the signature recorded in the public transparency log; see SECURITY.md).
The updater checks that signature itself, with the standard library and the `cryptography` package the image
already carries, against the Sigstore trust data shipped in the image (`sigstore_trusted_root.json`):

  1. the registry names the digest of the version tag; the signature bundle is found through the referrers of that
     digest (OCI referrers API, or the tag fallback `sha256-<hex>` the registry offers instead);
  2. the bundle's certificate chains to a Sigstore certificate authority of the trust data, carries a valid
     certificate-transparency timestamp, may sign code, and names the expected workflow identity and OIDC issuer;
  3. the bundle's signed statement (DSSE, in-toto) names exactly this digest, and its signature checks out with the
     certificate's key;
  4. the transparency log entry belongs to this signature (same payload, signature and certificate), its signed entry
     timestamp checks out with a log key of the trust data, it was recorded while the certificate was valid, and an
     inclusion proof with its signed checkpoint, when present, leads to the same tree.

Only then does the updater pull, and it pulls by that digest, so the bytes it runs are the bytes that were checked.

Results are reported with i18n keys:
  update.err.signature_missing     no signature bundle from the publishing workflow for this digest
  update.err.signature_invalid     a bundle exists but does not check out (signer, digest, signature, log entry)
  update.err.signature_unchecked   the registry could not be asked (network); nothing is decided
  update.err.signature_trust       the bundle names a log or authority this version's trust data does not know
                                   (the signing service renewed its keys): one manual update brings new trust data

The trust data is the public-good Sigstore trusted root (TUF target `trusted_root.json`, as fetched and verified by
the signing tool; source: github.com/sigstore/root-signing, file targets/trusted_root.json). The release gate runs
`python -m updater --verify <image>` against every published image, so a release whose signature this code cannot
check is never published.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:      # part of the product image; a stripped image without it can still update local test images (no check)
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519
    from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID
    AVAILABLE = True
except ImportError:  # pragma: no cover - the product image always has it
    x509 = InvalidSignature = hashes = serialization = ec = ed25519 = ExtendedKeyUsageOID = ExtensionOID = None
    AVAILABLE = False

OFFICIAL_REPO = "ghcr.io/robeertm/lanloom"
IDENTITY = "https://github.com/robeertm/lanloom/.github/workflows/publish.yml@refs/heads/main"
ISSUER = "https://token.actions.githubusercontent.com"
TRUSTED_ROOT = Path(__file__).resolve().with_name("sigstore_trusted_root.json")

BUNDLE_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
BUNDLE_TYPES = (BUNDLE_TYPE, "application/vnd.dev.sigstore.bundle+json;version=0.3")
SIGN_PREDICATE = "https://sigstore.dev/cosign/sign/v1"
INTOTO_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPES = ("https://in-toto.io/Statement/v1", "https://in-toto.io/Statement/v0.1")
OID_ISSUER_V1 = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.1") if AVAILABLE else None
OID_ISSUER_V2 = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.8") if AVAILABLE else None

MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])
DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
TAG_RE = re.compile(r"\A[\w][\w.-]{0,127}\Z")
MANIFEST_MAX = 4 * 1024 * 1024
BUNDLE_MAX = 1024 * 1024
TOKEN_MAX = 64 * 1024
MAX_BUNDLES = 8
TIMEOUT = 20.0

MISSING = "update.err.signature_missing"
INVALID = "update.err.signature_invalid"
UNCHECKED = "update.err.signature_unchecked"
TRUST = "update.err.signature_trust"
KEYS = (MISSING, INVALID, UNCHECKED, TRUST)


class SignatureError(Exception):
    """A failed check with its i18n key and a short detail for the updater log (no secrets, no bundle content)."""

    def __init__(self, key: str, detail: str):
        super().__init__(detail)
        self.key = key
        self.detail = detail


@dataclass
class Verified:
    digest: str
    identity: str
    issuer: str
    log_index: int
    integrated_time: int

    def as_status(self) -> dict:
        return {"verified": True, "identity": self.identity, "digest": self.digest, "log_index": self.log_index,
                "signed": self.integrated_time}


# ------------------------------------------------------------------ trust data
def _time(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _valid_for(obj: dict) -> tuple[float, float]:
    vf = obj.get("validFor") or {}
    start = _time(vf.get("start"))
    end = _time(vf.get("end"))
    return (start if start is not None else 0.0, end if end is not None else float("inf"))


def _b64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("not a base64 string")
    return base64.b64decode(value, validate=True)


@dataclass
class TrustRoot:
    tlogs: dict = field(default_factory=dict)       # log id (bytes) -> (public key, start, end)
    ctlogs: dict = field(default_factory=dict)      # log id (bytes) -> (public key, start, end)
    cas: list = field(default_factory=list)         # (chain [intermediate ..., root], start, end)

    @classmethod
    def load(cls, path: Path = TRUSTED_ROOT) -> "TrustRoot":
        return cls.parse(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def parse(cls, doc: dict) -> "TrustRoot":
        root = cls()
        for name, target in (("tlogs", root.tlogs), ("ctlogs", root.ctlogs)):
            for log in doc.get(name) or []:
                try:
                    pk = log["publicKey"]
                    key = serialization.load_der_public_key(_b64(pk["rawBytes"]))
                    log_id = _b64(log["logId"]["keyId"])
                except (KeyError, TypeError, ValueError):
                    continue
                target[log_id] = (key, *_valid_for(pk))
        for ca in doc.get("certificateAuthorities") or []:
            try:
                chain = [x509.load_der_x509_certificate(_b64(c["rawBytes"]))
                         for c in (ca.get("certChain") or {}).get("certificates") or []]
            except (KeyError, TypeError, ValueError):
                continue
            if chain:
                root.cas.append((chain, *_valid_for(ca)))
        if not root.tlogs or not root.cas:
            raise ValueError("trust data without transparency log or certificate authority")
        return root


# ------------------------------------------------------------------ primitives
def _verify_sig(key: Any, signature: bytes, data: bytes) -> bool:
    try:
        if isinstance(key, ec.EllipticCurvePublicKey):
            algo = hashes.SHA384() if key.curve.key_size == 384 else (
                hashes.SHA512() if key.curve.key_size > 384 else hashes.SHA256())
            key.verify(signature, data, ec.ECDSA(algo))
        elif isinstance(key, ed25519.Ed25519PublicKey):
            key.verify(signature, data)
        else:
            return False
        return True
    except (InvalidSignature, ValueError):
        return False


def _der_utf8(value: bytes) -> str | None:
    """A DER UTF8String (tag 0x0c) -> text; anything else -> None."""
    if len(value) < 2 or value[0] != 0x0C:
        return None
    n, i = value[1], 2
    if n & 0x80:
        size = n & 0x7F
        if size == 0 or size > 4 or len(value) < 2 + size:
            return None
        n, i = int.from_bytes(value[2:2 + size], "big"), 2 + size
    if len(value) != i + n:
        return None
    try:
        return value[i:].decode("utf-8")
    except UnicodeDecodeError:
        return None


def cert_issuer(cert: x509.Certificate) -> str | None:
    """OIDC issuer named by a Sigstore certificate (current extension, then the legacy one)."""
    for oid, decode in ((OID_ISSUER_V2, _der_utf8), (OID_ISSUER_V1, lambda b: b.decode("utf-8", "replace"))):
        try:
            ext = cert.extensions.get_extension_for_oid(oid)
        except x509.ExtensionNotFound:
            continue
        raw = getattr(ext.value, "value", None)
        if isinstance(raw, bytes):
            text = decode(raw)
            if text:
                return text
    return None


def cert_identities(cert: x509.Certificate) -> list[str]:
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    except x509.ExtensionNotFound:
        return []
    return [str(v) for v in san.get_values_for_type(x509.UniformResourceIdentifier)] + \
           [str(v) for v in san.get_values_for_type(x509.RFC822Name)]


def _utc(dt: datetime) -> float:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE pre-authentication encoding."""
    t = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(payload), payload)


def merkle_root(index: int, size: int, leaf_hash: bytes, proof: list[bytes]) -> bytes:
    """Root of an RFC 9162 inclusion proof (raises ValueError when the proof does not fit the tree)."""
    if index < 0 or index >= size:
        raise ValueError("index outside the tree")
    fn, sn, r = index, size - 1, leaf_hash
    for p in proof:
        if sn == 0:
            raise ValueError("proof too long")
        if fn & 1 or fn == sn:
            r = hashlib.sha256(b"\x01" + p + r).digest()
            if not fn & 1:
                while fn and not fn & 1:
                    fn >>= 1
                    sn >>= 1
        else:
            r = hashlib.sha256(b"\x01" + r + p).digest()
        fn >>= 1
        sn >>= 1
    if sn != 0:
        raise ValueError("proof too short")
    return r


# ------------------------------------------------------------------ checks of one bundle
def _check_chain(leaf: x509.Certificate, root: TrustRoot, at: float) -> None:
    for chain, start, end in root.cas:
        if not start <= at <= end:
            continue
        try:
            leaf.verify_directly_issued_by(chain[0])
            for child, parent in zip(chain, chain[1:]):
                child.verify_directly_issued_by(parent)
            chain[-1].verify_directly_issued_by(chain[-1])    # the anchor is self-signed
        except (ValueError, TypeError, InvalidSignature):
            continue
        return
    raise SignatureError(TRUST, "certificate does not chain to a known certificate authority")


def _check_sct(leaf: x509.Certificate, issuer: x509.Certificate, root: TrustRoot) -> None:
    try:
        scts = leaf.extensions.get_extension_for_oid(ExtensionOID.PRECERT_SIGNED_CERTIFICATE_TIMESTAMPS).value
    except x509.ExtensionNotFound:
        raise SignatureError(INVALID, "certificate without transparency timestamp") from None
    issuer_key_hash = hashlib.sha256(issuer.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).digest()
    tbs = leaf.tbs_precertificate_bytes
    unknown = False
    for sct in scts:
        log = root.ctlogs.get(sct.log_id)
        if log is None:
            unknown = True
            continue
        key, start, end = log
        ts_ms = int(round(_utc(sct.timestamp) * 1000))
        if not start <= ts_ms / 1000 <= end:
            continue
        ext = getattr(sct, "extension_bytes", b"") or b""
        signed = (b"\x00" + b"\x00" + ts_ms.to_bytes(8, "big") + b"\x00\x01" + issuer_key_hash
                  + len(tbs).to_bytes(3, "big") + tbs + len(ext).to_bytes(2, "big") + ext)
        if _verify_sig(key, sct.signature, signed):
            return
    if unknown:
        raise SignatureError(TRUST, "transparency timestamp from an unknown log")
    raise SignatureError(INVALID, "transparency timestamp does not check out")


def _check_leaf(leaf: x509.Certificate, identity: str, issuer: str) -> None:
    try:
        eku = leaf.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE).value
        if ExtendedKeyUsageOID.CODE_SIGNING not in eku:
            raise SignatureError(INVALID, "certificate is not for code signing")
        ku = leaf.extensions.get_extension_for_oid(ExtensionOID.KEY_USAGE).value
        if not ku.digital_signature:
            raise SignatureError(INVALID, "certificate key may not sign")
    except x509.ExtensionNotFound:
        raise SignatureError(INVALID, "certificate without key usage") from None
    names = cert_identities(leaf)
    if identity not in names:
        raise SignatureError(INVALID, "signed by another identity")
    if cert_issuer(leaf) != issuer:
        raise SignatureError(INVALID, "certificate from another OIDC issuer")


def _issuer_of(leaf: x509.Certificate, root: TrustRoot) -> x509.Certificate:
    for chain, _s, _e in root.cas:
        try:
            leaf.verify_directly_issued_by(chain[0])
            return chain[0]
        except (ValueError, TypeError, InvalidSignature):
            continue
    raise SignatureError(TRUST, "issuer of the certificate unknown")


def _check_tlog(entry: dict, root: TrustRoot, payload: bytes, sig_b64: str, leaf_der: bytes,
                leaf: x509.Certificate) -> tuple[int, int]:
    try:
        log_id = _b64(entry["logId"]["keyId"])
        log_index = int(entry["logIndex"])
        integrated = int(entry["integratedTime"])
        body_b64 = entry["canonicalizedBody"]
        body_raw = _b64(body_b64)
        body = json.loads(body_raw)
        kind = entry.get("kindVersion") or {}
    except (KeyError, TypeError, ValueError):
        raise SignatureError(INVALID, "malformed transparency log entry") from None
    log = root.tlogs.get(log_id)
    if log is None:
        raise SignatureError(TRUST, "transparency log unknown to this version")
    key, start, end = log
    if not start <= integrated <= end:
        raise SignatureError(TRUST, "transparency log entry outside the log's validity")
    # the entry must describe exactly this signature: same payload, signature and certificate
    if kind.get("kind") != "dsse" or body.get("kind") != "dsse":
        raise SignatureError(INVALID, "transparency log entry of another kind")
    spec = body.get("spec") or {}
    ph = (spec.get("payloadHash") or {})
    if ph.get("algorithm") != "sha256" or ph.get("value") != hashlib.sha256(payload).hexdigest():
        raise SignatureError(INVALID, "transparency log entry for another statement")
    match = False
    for s in spec.get("signatures") or []:
        try:
            verifier = _b64(s.get("verifier"))
            if b"-----BEGIN" in verifier:
                verifier = x509.load_pem_x509_certificate(verifier).public_bytes(serialization.Encoding.DER)
        except (ValueError, TypeError):
            continue
        if s.get("signature") == sig_b64 and verifier == leaf_der:
            match = True
    if not match:
        raise SignatureError(INVALID, "transparency log entry for another signature")
    promise = (entry.get("inclusionPromise") or {}).get("signedEntryTimestamp")
    if not promise:
        raise SignatureError(TRUST, "transparency log entry without signed entry timestamp")
    canonical = json.dumps({"body": body_b64, "integratedTime": integrated, "logID": log_id.hex(),
                            "logIndex": log_index}, sort_keys=True, separators=(",", ":")).encode()
    try:
        set_sig = _b64(promise)
    except (ValueError, binascii.Error):
        raise SignatureError(INVALID, "malformed signed entry timestamp") from None
    if not _verify_sig(key, set_sig, canonical):
        raise SignatureError(INVALID, "signed entry timestamp does not check out")
    if not _utc(leaf.not_valid_before_utc) <= integrated <= _utc(leaf.not_valid_after_utc):
        raise SignatureError(INVALID, "signature recorded outside the certificate's validity")
    proof = entry.get("inclusionProof")
    if proof:
        _check_inclusion(proof, body_raw, key)
    return log_index, integrated


def _check_inclusion(proof: dict, body_raw: bytes, key: Any) -> None:
    try:
        index = int(proof["logIndex"])
        size = int(proof["treeSize"])
        root_hash = _b64(proof["rootHash"])
        hashes_ = [_b64(h) for h in proof.get("hashes") or []]
    except (KeyError, TypeError, ValueError):
        raise SignatureError(INVALID, "malformed inclusion proof") from None
    try:
        calc = merkle_root(index, size, hashlib.sha256(b"\x00" + body_raw).digest(), hashes_)
    except ValueError as exc:
        raise SignatureError(INVALID, "inclusion proof: %s" % exc) from None
    if calc != root_hash:
        raise SignatureError(INVALID, "inclusion proof leads to another tree")
    envelope = (proof.get("checkpoint") or {}).get("envelope")
    if envelope:
        _check_checkpoint(envelope, size, root_hash, key)


def _check_checkpoint(envelope: str, size: int, root_hash: bytes, key: Any) -> None:
    """A signed note: '<origin>\\n<size>\\n<root b64>\\n[other lines]\\n\\n— <name> <b64(key hint + signature)>\\n'."""
    text, sep, sigs = envelope.partition("\n\n")
    lines = text.split("\n")
    if not sep or len(lines) < 3:
        raise SignatureError(INVALID, "malformed checkpoint")
    try:
        if int(lines[1]) != size or _b64(lines[2]) != root_hash:
            raise SignatureError(INVALID, "checkpoint for another tree")
    except ValueError:
        raise SignatureError(INVALID, "malformed checkpoint") from None
    message = (text + "\n").encode()
    for line in sigs.split("\n"):
        if not line.startswith("— "):
            continue
        try:
            raw = _b64(line.rsplit(" ", 1)[-1])
        except (ValueError, binascii.Error):
            continue
        if len(raw) > 4 and _verify_sig(key, raw[4:], message):
            return
    raise SignatureError(INVALID, "checkpoint signature does not check out")


def verify_bundle(bundle: Any, digest: str, root: TrustRoot, identity: str = IDENTITY,
                  issuer: str = ISSUER) -> Verified:
    """Check one Sigstore bundle against the image digest. Raises SignatureError."""
    if not isinstance(bundle, dict) or bundle.get("mediaType") not in BUNDLE_TYPES:
        raise SignatureError(INVALID, "not a signature bundle of a known version")
    if not DIGEST_RE.match(digest or ""):
        raise SignatureError(INVALID, "digest is not sha256")
    vm = bundle.get("verificationMaterial") or {}
    env = bundle.get("dsseEnvelope")
    try:
        leaf_der = _b64((vm.get("certificate") or {}).get("rawBytes"))
        leaf = x509.load_der_x509_certificate(leaf_der)
    except (ValueError, TypeError):
        raise SignatureError(INVALID, "bundle without a readable certificate") from None
    if not isinstance(env, dict) or env.get("payloadType") != INTOTO_TYPE:
        raise SignatureError(INVALID, "bundle without a signed statement")
    try:
        payload = _b64(env.get("payload"))
        statement = json.loads(payload)
    except (ValueError, TypeError):
        raise SignatureError(INVALID, "unreadable statement") from None
    entries = vm.get("tlogEntries") or []
    if not isinstance(entries, list) or not entries:
        raise SignatureError(TRUST, "bundle without transparency log entry")

    # who signed
    _check_leaf(leaf, identity, issuer)
    # what was signed: the statement names this digest and is the plain signature statement
    if not isinstance(statement, dict) or statement.get("_type") not in STATEMENT_TYPES \
            or statement.get("predicateType") != SIGN_PREDICATE:
        raise SignatureError(INVALID, "statement is not an image signature")
    hexd = digest.split(":", 1)[1]
    if not any(isinstance(s, dict) and (s.get("digest") or {}).get("sha256") == hexd
               for s in statement.get("subject") or []):
        raise SignatureError(INVALID, "signature for another image")
    # the signature itself
    key = leaf.public_key()
    good_sig = None
    for s in env.get("signatures") or []:
        try:
            raw = _b64(s.get("sig"))
        except (ValueError, TypeError, AttributeError):
            continue
        if _verify_sig(key, raw, pae(INTOTO_TYPE, payload)):
            good_sig = s.get("sig")
            break
    if good_sig is None:
        raise SignatureError(INVALID, "signature does not check out")
    # when, and recorded publicly (the time also decides which authority and certificate validity apply)
    errors: list[SignatureError] = []
    for entry in entries[:4]:
        try:
            log_index, integrated = _check_tlog(entry, root, payload, good_sig, leaf_der, leaf)
        except SignatureError as exc:
            errors.append(exc)
            continue
        _check_chain(leaf, root, integrated)
        _check_sct(leaf, _issuer_of(leaf, root), root)
        return Verified(digest=digest, identity=identity, issuer=issuer, log_index=log_index,
                        integrated_time=integrated)
    invalid = [e for e in errors if e.key == INVALID]
    raise (invalid or errors)[0]


# ------------------------------------------------------------------ registry
Opener = Callable[[str, dict], tuple[int, dict, bytes]]


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follows redirects (the registry hands blobs to a storage host) only to https; the token never travels along
    (it is an unredirected header)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_opener(url: str, headers: dict, max_bytes: int = MANIFEST_MAX) -> tuple[int, dict, bytes]:
    """GET over https only. Returns (status, lower-case headers, body capped at max_bytes + 1)."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise SignatureError(UNCHECKED, "registry address is not https")
    req = urllib.request.Request(url, method="GET")
    for name, value in headers.items():
        if name.lower() == "authorization":
            req.add_unredirected_header(name, value)
        else:
            req.add_header(name, value)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                                         _NoAuthRedirect())
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, b""


def _challenge(value: str) -> dict:
    """Parse 'Bearer realm="…",service="…",scope="…"'."""
    if not value.lower().startswith("bearer "):
        return {}
    return {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', value[7:])}


class Registry:
    """Anonymous, read-only access to one repository of an OCI registry (manifests and blobs by digest)."""

    def __init__(self, repo: str, opener: Opener | None = None):
        host, _, path = repo.partition("/")
        if not host or not path:
            raise SignatureError(UNCHECKED, "repository without registry host")
        self.host, self.path = host, path
        self.opener = opener or (lambda url, headers: default_opener(url, headers))
        self.token: str | None = None

    def _url(self, tail: str) -> str:
        return "https://%s/v2/%s/%s" % (self.host, self.path, tail)

    def _get(self, url: str, accept: str, limit: int) -> tuple[int, dict, bytes]:
        try:
            headers = {"Accept": accept, "User-Agent": "lanloom-updater"}
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            status, rh, body = self.opener(url, headers)
            if status == 401 and not self.token:
                self._login(rh.get("www-authenticate", ""))
                headers["Authorization"] = "Bearer " + (self.token or "")
                status, rh, body = self.opener(url, headers)
        except SignatureError:
            raise
        except (OSError, ValueError) as exc:
            raise SignatureError(UNCHECKED, "registry not reachable: %s" % type(exc).__name__) from None
        if len(body) > limit:
            raise SignatureError(INVALID, "registry answer too large")
        return status, rh, body

    def _login(self, challenge: str) -> None:
        ch = _challenge(challenge)
        realm = ch.get("realm", "")
        if urllib.parse.urlsplit(realm).scheme != "https":
            raise SignatureError(UNCHECKED, "registry asks for an unusable login")
        query = {"scope": "repository:%s:pull" % self.path}
        if ch.get("service"):
            query["service"] = ch["service"]
        status, _h, body = self.opener(realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(query),
                                       {"Accept": "application/json", "User-Agent": "lanloom-updater"})
        if status != 200 or len(body) > TOKEN_MAX:
            raise SignatureError(UNCHECKED, "registry refused an anonymous token (%s)" % status)
        try:
            data = json.loads(body)
            self.token = str(data.get("token") or data.get("access_token") or "")
        except ValueError:
            raise SignatureError(UNCHECKED, "registry token unreadable") from None
        if not self.token:
            raise SignatureError(UNCHECKED, "registry gave no token")

    def manifest(self, ref: str) -> tuple[str, dict]:
        """(digest, document) of a tag or digest; the digest is computed from the bytes received."""
        status, rh, body = self._get(self._url("manifests/" + ref), MANIFEST_TYPES, MANIFEST_MAX)
        if status == 404:
            raise SignatureError(MISSING, "nothing published under %s" % ref[:80])
        if status != 200:
            raise SignatureError(UNCHECKED, "registry answered %s" % status)
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if DIGEST_RE.match(ref) and ref != digest:
            raise SignatureError(INVALID, "registry sent another manifest than asked for")
        sent = rh.get("docker-content-digest")
        if sent and sent != digest:
            raise SignatureError(INVALID, "registry digest does not match the manifest it sent")
        try:
            doc = json.loads(body)
        except ValueError:
            raise SignatureError(INVALID, "manifest unreadable") from None
        return digest, doc if isinstance(doc, dict) else {}

    def blob(self, digest: str, limit: int) -> bytes:
        status, _h, body = self._get(self._url("blobs/" + digest), "*/*", limit)
        if status != 200:
            raise SignatureError(UNCHECKED if status >= 500 else MISSING, "blob not available (%s)" % status)
        if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
            raise SignatureError(INVALID, "blob does not match its digest")
        return body

    def referrers(self, digest: str) -> list[dict]:
        """Descriptors referring to a digest: the referrers API, else the tag fallback 'sha256-<hex>'."""
        status, _h, body = self._get(self._url("referrers/" + digest), "application/vnd.oci.image.index.v1+json",
                                     MANIFEST_MAX)
        docs = []
        if status == 200:
            try:
                docs.append(json.loads(body))
            except ValueError:
                pass
        if not docs:
            try:
                docs.append(self.manifest("sha256-" + digest.split(":", 1)[1])[1])
            except SignatureError as exc:
                if exc.key != MISSING:
                    raise
        out = []
        for doc in docs:
            for m in (doc or {}).get("manifests") or []:
                if isinstance(m, dict) and m.get("artifactType") in BUNDLE_TYPES:
                    out.append(m)
        return out


def find_bundles(reg: Registry, digest: str) -> list[dict]:
    """Signature bundles (image signatures, not SBOM attestations) attached to a digest."""
    descs = reg.referrers(digest)
    sign = [d for d in descs if (d.get("annotations") or {}).get("dev.sigstore.bundle.predicateType") == SIGN_PREDICATE]
    rest = [d for d in descs if d not in sign and not (d.get("annotations") or {}).get("dev.sigstore.bundle.predicateType")]
    bundles = []
    for desc in (sign + rest)[:MAX_BUNDLES]:
        if not DIGEST_RE.match(str(desc.get("digest") or "")):
            continue
        _d, man = reg.manifest(desc["digest"])
        for layer in man.get("layers") or []:
            if layer.get("mediaType") in BUNDLE_TYPES and DIGEST_RE.match(str(layer.get("digest") or "")):
                if int(layer.get("size") or 0) > BUNDLE_MAX:
                    continue
                try:
                    bundles.append(json.loads(reg.blob(layer["digest"], BUNDLE_MAX)))
                except ValueError:
                    continue
    return bundles


def verify_image(repo: str, tag: str, root: TrustRoot | None = None, opener: Opener | None = None,
                 identity: str = IDENTITY, issuer: str = ISSUER) -> Verified:
    """Resolve repo:tag to its digest and check its signature. Raises SignatureError."""
    if not AVAILABLE:
        raise SignatureError(TRUST, "signature library missing in this updater image")
    if not TAG_RE.match(tag or "") and not DIGEST_RE.match(tag or ""):
        raise SignatureError(INVALID, "not a tag")
    try:
        root = root or TrustRoot.load()
    except (OSError, ValueError):
        raise SignatureError(TRUST, "trust data of this version unreadable") from None
    reg = Registry(repo, opener)
    digest, _doc = reg.manifest(tag)
    bundles = find_bundles(reg, digest)
    if not bundles:
        raise SignatureError(MISSING, "no signature for %s" % digest[:19])
    errors: list[SignatureError] = []
    for b in bundles:
        try:
            return verify_bundle(b, digest, root, identity, issuer)
        except SignatureError as exc:
            errors.append(exc)
    for key in (INVALID, TRUST):
        hit = [e for e in errors if e.key == key]
        if hit:
            raise hit[0]
    raise errors[0]
