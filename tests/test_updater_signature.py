"""Signature check of an update candidate (updater/sigverify.py) and how the engine uses it.

The bundle tests use a real signature of a published image (tests/fixtures/signed-image-registry.json, public data)
and the Sigstore trust data shipped in the image; every tampered variant must be refused with the right key. The
engine tests use the fake container runtime of test_updater_engine.py: a failed check changes nothing, a passed check
pulls exactly the checked digest."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from updater import engine as E  # noqa: E402
from updater import sigverify as S  # noqa: E402
from test_updater_engine import NONCE, FakeRuntime, fast_settings, make_db, marker  # noqa: E402

FIX = json.loads((Path(__file__).parent / "fixtures" / "signed-image-registry.json").read_text())
FILES = {k: base64.b64decode(v) for k, v in FIX["files"].items()}
DIGEST = FIX["digest"]
TAG = FIX["tag"]
OFFICIAL = S.OFFICIAL_REPO


@pytest.fixture(scope="module")
def root():
    return S.TrustRoot.load()


def bundle():
    return json.loads(FILES["bundle"])


def flip(b64: str) -> str:
    raw = bytearray(base64.b64decode(b64))
    raw[len(raw) // 2] ^= 0x01
    return base64.b64encode(bytes(raw)).decode()


def refused(b, root, key, digest=DIGEST, **kw):
    with pytest.raises(S.SignatureError) as exc:
        S.verify_bundle(b, digest, root, **kw)
    assert exc.value.key == key, exc.value.detail
    return exc.value.detail


# ------------------------------------------------------------------ trust data
def test_trust_data_is_the_public_sigstore_root():
    raw = S.TRUSTED_ROOT.read_bytes()
    doc = json.loads(raw)
    assert doc["mediaType"].startswith("application/vnd.dev.sigstore.trustedroot+json")
    root = S.TrustRoot.parse(doc)
    # the transparency log, certificate authority and certificate-transparency log that sign today
    assert base64.b64decode("wNI9atQGlz+VWfO6LRygH4QUfY/8W4RFwiT5i5WRgB0=") in root.tlogs
    assert base64.b64decode("3T0wasbHETJjGR4cmWc3AqJKXrjePK3/h4pygC8p7o4=") in root.ctlogs
    assert any(chain[-1].subject.rfc4514_string() == "CN=sigstore,O=sigstore.dev" for chain, _s, _e in root.cas)


def test_trust_data_without_log_or_authority_is_refused():
    with pytest.raises(ValueError):
        S.TrustRoot.parse({"tlogs": [], "certificateAuthorities": []})


# ------------------------------------------------------------------ bundle
def test_real_signature_checks_out(root):
    v = S.verify_bundle(bundle(), DIGEST, root)
    assert v.digest == DIGEST and v.identity == S.IDENTITY and v.issuer == S.ISSUER
    assert v.log_index > 0 and v.integrated_time > 1.7e9
    st = v.as_status()
    assert st["verified"] is True and st["digest"] == DIGEST


def test_signature_for_another_image(root):
    assert "another image" in refused(bundle(), root, S.INVALID, digest="sha256:" + "0" * 64)


def test_other_identity_or_issuer(root):
    refused(bundle(), root, S.INVALID, identity="https://github.com/someone/fork/.github/workflows/publish.yml@refs/heads/main")
    refused(bundle(), root, S.INVALID, identity=S.IDENTITY.replace("refs/heads/main", "refs/heads/feature"))
    refused(bundle(), root, S.INVALID, issuer="https://issuer.example")


def test_broken_signature(root):
    b = bundle()
    b["dsseEnvelope"]["signatures"][0]["sig"] = flip(b["dsseEnvelope"]["signatures"][0]["sig"])
    assert "signature" in refused(b, root, S.INVALID)


def test_altered_statement(root):
    b = bundle()
    stmt = json.loads(base64.b64decode(b["dsseEnvelope"]["payload"]))
    stmt["predicate"] = {"note": "changed"}
    b["dsseEnvelope"]["payload"] = base64.b64encode(json.dumps(stmt).encode()).decode()
    refused(b, root, S.INVALID)
    b = bundle()
    stmt = json.loads(base64.b64decode(b["dsseEnvelope"]["payload"]))
    stmt["predicateType"] = "https://spdx.dev/Document"
    b["dsseEnvelope"]["payload"] = base64.b64encode(json.dumps(stmt).encode()).decode()
    assert "not an image signature" in refused(b, root, S.INVALID)


def test_broken_entry_timestamp(root):
    b = bundle()
    e = b["verificationMaterial"]["tlogEntries"][0]
    e["inclusionPromise"]["signedEntryTimestamp"] = flip(e["inclusionPromise"]["signedEntryTimestamp"])
    assert "entry timestamp" in refused(b, root, S.INVALID)
    b = bundle()
    b["verificationMaterial"]["tlogEntries"][0]["integratedTime"] = str(int(time.time()))
    refused(b, root, S.INVALID)


def test_log_entry_for_another_signature(root):
    b = bundle()
    e = b["verificationMaterial"]["tlogEntries"][0]
    body = json.loads(base64.b64decode(e["canonicalizedBody"]))
    body["spec"]["payloadHash"]["value"] = hashlib.sha256(b"other").hexdigest()
    e["canonicalizedBody"] = base64.b64encode(json.dumps(body).encode()).decode()
    assert "another statement" in refused(b, root, S.INVALID)


def test_broken_inclusion_proof_and_checkpoint(root):
    b = bundle()
    p = b["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]
    p["hashes"][0] = flip(p["hashes"][0])
    assert "inclusion proof" in refused(b, root, S.INVALID)
    b = bundle()
    cp = b["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]["checkpoint"]
    head, _, sig = cp["envelope"].partition("\n\n")
    name, _, value = sig.strip().rpartition(" ")
    cp["envelope"] = head + "\n\n" + name + " " + flip(value) + "\n"
    assert "checkpoint" in refused(b, root, S.INVALID)


def test_unknown_log_authority_or_ct_log_is_a_trust_problem(root):
    r = copy.copy(root)
    r.tlogs = {}
    refused(bundle(), r, S.TRUST)
    r = copy.copy(root)
    r.cas = []
    refused(bundle(), r, S.TRUST)
    r = copy.copy(root)
    r.ctlogs = {}
    refused(bundle(), r, S.TRUST)


def test_bundle_without_log_entry_or_certificate(root):
    b = bundle()
    b["verificationMaterial"]["tlogEntries"] = []
    refused(b, root, S.TRUST)
    b = bundle()
    b["verificationMaterial"]["certificate"]["rawBytes"] = "AAAA"
    refused(b, root, S.INVALID)
    refused({"mediaType": "something else"}, root, S.INVALID)
    refused("not even a dict", root, S.INVALID)


def test_merkle_root_small_tree():
    h = lambda b: hashlib.sha256(b).digest()  # noqa: E731
    leaves = [h(b"\x00" + bytes([i])) for i in range(5)]
    n01, n23 = h(b"\x01" + leaves[0] + leaves[1]), h(b"\x01" + leaves[2] + leaves[3])
    n0123 = h(b"\x01" + n01 + n23)
    top = h(b"\x01" + n0123 + leaves[4])
    assert S.merkle_root(2, 5, leaves[2], [leaves[3], n01, leaves[4]]) == top
    assert S.merkle_root(4, 5, leaves[4], [n0123]) == top
    with pytest.raises(ValueError):
        S.merkle_root(4, 5, leaves[4], [n0123, n01])
    with pytest.raises(ValueError):
        S.merkle_root(5, 5, leaves[4], [])


def test_issuer_extension_parsing():
    assert S._der_utf8(b"\x0c\x05hello") == "hello"
    assert S._der_utf8(b"\x0c\x81\x03abc") == "abc"
    assert S._der_utf8(b"\x0c\x06hello") is None
    assert S._der_utf8(b"\x13\x05hello") is None


# ------------------------------------------------------------------ registry
class FakeRegistry:
    """Answers like the image registry of the project (token challenge, no referrers API, tag fallback)."""

    def __init__(self, files=None):
        self.files = dict(FILES if files is None else files)
        self.seen: list[tuple[str, dict]] = []
        self.down = False
        self.wrong_digest_header = False

    def __call__(self, url: str, headers: dict):
        self.seen.append((url, dict(headers)))
        if self.down:
            raise OSError("network unreachable")
        if url.startswith("https://ghcr.io/token?"):
            assert "scope=repository%3Arobeertm%2Flanloom%3Apull" in url
            return 200, {}, b'{"token":"anon-test-token"}'
        if headers.get("Authorization") != "Bearer anon-test-token":
            return 401, {"www-authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="x"'}, b""
        base = "https://ghcr.io/v2/robeertm/lanloom/"
        tail = url[len(base):]
        index_digest = "sha256:" + hashlib.sha256(self.files["index"]).hexdigest()
        man_digest = "sha256:" + hashlib.sha256(self.files["bundle_manifest"]).hexdigest()
        if tail in ("manifests/" + TAG, "manifests/" + index_digest):
            hdr = {"docker-content-digest": "sha256:" + "1" * 64 if self.wrong_digest_header else index_digest}
            return 200, hdr, self.files["index"]
        if tail.startswith("referrers/"):
            return 404, {}, b""
        if tail == "manifests/sha256-" + DIGEST.split(":")[1] and "fallback" in self.files:
            return 200, {}, self.files["fallback"]
        if tail == "manifests/" + man_digest:
            return 200, {}, self.files["bundle_manifest"]
        if tail.startswith("blobs/"):
            return 200, {}, self.files["bundle"]
        return 404, {}, b""


def test_verify_image_through_the_registry(root):
    reg = FakeRegistry()
    v = S.verify_image(OFFICIAL, TAG, root, opener=reg)
    assert v.digest == DIGEST
    # anonymous token after the challenge, then every request with it; the SBOM attestations are not downloaded
    assert reg.seen[0][0].endswith("/manifests/" + TAG) and "Authorization" not in reg.seen[0][1]
    assert any(u.startswith("https://ghcr.io/token?") for u, _ in reg.seen)
    blobs = [u for u, _ in reg.seen if "/blobs/" in u]
    assert len(blobs) == 1


def test_verify_image_missing_unchecked_and_tampered(root):
    reg = FakeRegistry()
    reg.files.pop("fallback")
    with pytest.raises(S.SignatureError) as exc:
        S.verify_image(OFFICIAL, TAG, root, opener=reg)
    assert exc.value.key == S.MISSING
    reg = FakeRegistry()
    reg.down = True
    with pytest.raises(S.SignatureError) as exc:
        S.verify_image(OFFICIAL, TAG, root, opener=reg)
    assert exc.value.key == S.UNCHECKED
    reg = FakeRegistry()
    reg.wrong_digest_header = True
    with pytest.raises(S.SignatureError) as exc:
        S.verify_image(OFFICIAL, TAG, root, opener=reg)
    assert exc.value.key == S.INVALID
    reg = FakeRegistry()
    reg.files["bundle"] = reg.files["bundle"].replace(b'"mediaType"', b'"mediaType" ')
    with pytest.raises(S.SignatureError) as exc:
        S.verify_image(OFFICIAL, TAG, root, opener=reg)
    assert exc.value.key == S.INVALID and "digest" in exc.value.detail
    with pytest.raises(S.SignatureError) as exc:
        S.verify_image(OFFICIAL, "../../etc", root, opener=FakeRegistry())
    assert exc.value.key == S.INVALID


def test_login_realm_must_be_https(root):
    def opener(url, headers):
        return 401, {"www-authenticate": 'Bearer realm="http://ghcr.io/token",service="ghcr.io"'}, b""
    with pytest.raises(S.SignatureError) as exc:
        S.verify_image(OFFICIAL, TAG, root, opener=opener)
    assert exc.value.key == S.UNCHECKED


def test_default_opener_refuses_plain_http():
    with pytest.raises(S.SignatureError) as exc:
        S.default_opener("http://registry.example/v2/", {})
    assert exc.value.key == S.UNCHECKED


# ------------------------------------------------------------------ engine
def official_env(tmp_path, verify_fn):
    data = tmp_path / "data"
    data.mkdir()
    make_db(data / "lanloom.db")
    fake = FakeRuntime(data)
    fake.add_image("1.0.0", repo=OFFICIAL)
    iid = fake.add_image("1.1.0", repo=OFFICIAL)
    # what a pull by digest leaves behind: the image under repo@digest (the version tag comes from the updater)
    fake.tags[f"{OFFICIAL}@{DIGEST}"] = iid
    del fake.tags[f"{OFFICIAL}:1.1.0"]
    old = fake.add_app("1.0.0", repo=OFFICIAL)
    upd = E.Updater(fake, fast_settings(data), health_fn=fake.health, echo=lambda line: None, verify_fn=verify_fn)
    return fake, upd, data, old


def send(upd, version="1.1.0", **extra) -> dict:
    upd.ensure_update_dir()
    upd.request_path.write_text(json.dumps({"version": version, "nonce": NONCE, "requested": time.time(), **extra}))
    assert upd.poll_once()
    return json.loads(upd.status_path.read_text())


def test_engine_pulls_exactly_the_checked_digest(tmp_path):
    seen = {}

    def verify(repo, tag):
        seen["call"] = (repo, tag)
        seen["step"] = upd.status.get("step")
        return S.Verified(digest=DIGEST, identity=S.IDENTITY, issuer=S.ISSUER, log_index=7, integrated_time=1)

    fake, upd, data, old = official_env(tmp_path, verify)
    st = send(upd)
    assert st["state"] == "done", st["log"]
    assert seen == {"call": (OFFICIAL, "1.1.0"), "step": "verify"}
    assert fake.pulls == [(OFFICIAL, DIGEST)]                      # by digest, never by tag
    assert fake.tags[f"{OFFICIAL}:1.1.0"] == fake.tags[f"{OFFICIAL}@{DIGEST}"]
    assert st["signature"]["verified"] is True and st["signature"]["digest"] == DIGEST
    assert st["step"] is None
    assert any("signature verified" in line for line in st["log"])
    # the heartbeat names the rollback target
    hb = json.loads(upd.heartbeat_path.read_text())
    assert hb["previous"] == "1.0.0" and "signature" in hb["features"]


@pytest.mark.parametrize("key", list(S.KEYS))
def test_failed_check_changes_nothing(tmp_path, key):
    def verify(repo, tag):
        raise S.SignatureError(key, "test")

    fake, upd, data, old = official_env(tmp_path, verify)
    st = send(upd)
    assert st["state"] == "failed" and st["message_key"] == key
    assert fake.pulls == []                                       # nothing pulled
    assert fake.by_name("lanloom")["Id"] == old                   # same container
    assert not (data / "backups").exists()
    assert marker(data / "lanloom.db") == "keep"
    assert st["step"] is None and key in E.MESSAGE_KEYS


def test_rollback_request_is_checked_like_an_update(tmp_path):
    calls = []

    def verify(repo, tag):
        calls.append(tag)
        raise S.SignatureError(S.MISSING, "unsigned old version")

    fake, upd, data, old = official_env(tmp_path, verify)
    fake.add_image("0.9.0", repo=OFFICIAL)
    st = send(upd, "0.9.0", rollback=True)
    assert calls == ["0.9.0"] and st["state"] == "failed" and st["message_key"] == S.MISSING
    assert fake.by_name("lanloom")["Id"] == old


def test_unofficial_repository_is_not_checked(tmp_path):
    from test_updater_engine import REPO

    data = tmp_path / "data"
    data.mkdir()
    make_db(data / "lanloom.db")
    fake = FakeRuntime(data)
    fake.add_image("1.0.0")
    fake.add_image("1.1.0")
    fake.add_app("1.0.0")

    def verify(repo, tag):
        raise AssertionError("must not be called")

    upd = E.Updater(fake, fast_settings(data), health_fn=fake.health, echo=lambda line: None, verify_fn=verify)
    st = send(upd)
    assert st["state"] == "done"
    assert st["signature"] == {"verified": False, "skipped": "unofficial"}
    assert fake.pulls == [(REPO, "1.1.0")]


def test_bench_reject_only_in_local_mode(tmp_path):
    s = E.Settings.from_env({"LANLOOM_UPDATER_TEST_REJECT": "1.0.4,bad"})
    assert s.test_reject == ()
    s = E.Settings.from_env({"LANLOOM_UPDATER_ALLOW_LOCAL": "1", "LANLOOM_UPDATER_TEST_REJECT": "1.0.4,bad"})
    assert s.test_reject == ("1.0.4",)
    data = tmp_path / "data"
    data.mkdir()
    make_db(data / "lanloom.db")
    fake = FakeRuntime(data)
    fake.add_image("1.0.0", repo="lanloom-bench")
    fake.add_image("1.0.4", repo="lanloom-bench")
    old = fake.add_app("1.0.0", repo="lanloom-bench")
    upd = E.Updater(fake, fast_settings(data, allow_local=True, test_reject=("1.0.4",)), health_fn=fake.health,
                    echo=lambda line: None)
    st = send(upd, "1.0.4")
    assert st["state"] == "failed" and st["message_key"] == "update.err.signature_invalid"
    assert fake.by_name("lanloom")["Id"] == old


def test_once_check_reports_signature_readiness(tmp_path):
    fake, upd, data, old = official_env(tmp_path, lambda r, t: None)
    rep = upd.check()
    assert rep["signature_required"] is True and rep["trust_data"] is True
