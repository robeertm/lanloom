#!/usr/bin/env python3
"""Keyless signatures and SBOM attestations of the published image (Sigstore: cosign + syft, pinned in
security/tools.json). There is no signing key anywhere: in the CI run cosign gets a short-lived certificate
for the workflow identity through OIDC, and the signature is recorded in the public transparency log.

  sign_image.py publish IMAGE@DIGEST|IMAGE:TAG --version V --tools DIR [--out DIR]
        (CI, publish.yml) sign the image index unless a valid signature exists, make one SPDX SBOM per platform
        and attach it as a signed attestation, then verify both
  sign_image.py verify IMAGE:TAG --tools DIR [--identity URI]
        signature of the index and one SBOM attestation per platform, both from the publishing workflow on main
  sign_image.py sboms IMAGE:TAG --tools DIR --out DIR
        verified SBOMs written as lanloom-<version>-<arch>.spdx.json (release assets)
Exit 0 = done and verified, 1 = missing or invalid signature/attestation, 2 = the tool itself failed.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ISSUER = "https://token.actions.githubusercontent.com"
REPO = os.environ.get("GITHUB_REPOSITORY") or "robeertm/lanloom"
IDENTITY = "https://github.com/%s/.github/workflows/publish.yml@refs/heads/main" % REPO
MEDIA = ", ".join(["application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
                   "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])


class Failed(Exception):
    pass


def split_ref(ref: str) -> tuple:
    m = re.match(r"^(ghcr\.io)/([a-z0-9._/-]+?)(?::([\w.-]+))?(?:@(sha256:[0-9a-f]{64}))?$", ref)
    if not m or not (m.group(3) or m.group(4)):
        raise Failed("not an image reference of ghcr.io with tag or digest: %s" % ref)
    return m.group(1), m.group(2), m.group(4) or m.group(3)


def registry_index(ref: str) -> tuple:
    """(index digest, {"linux/amd64": digest, ...}) straight from the registry (anonymous, standard library)."""
    host, repo, tag = split_ref(ref)
    with urllib.request.urlopen("https://%s/token?scope=repository:%s:pull" % (host, repo), timeout=30) as r:
        token = json.load(r)["token"]
    req = urllib.request.Request("https://%s/v2/%s/manifests/%s" % (host, repo, tag),
                                 headers={"Authorization": "Bearer " + token, "Accept": MEDIA})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read(4 * 1024 * 1024)
        digest = r.headers.get("docker-content-digest") or "sha256:" + hashlib.sha256(body).hexdigest()
    if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise Failed("registry digest does not match the manifest it sent")
    doc = json.loads(body)
    plats = {}
    for m in doc.get("manifests") or []:
        p = m.get("platform") or {}
        if p.get("os") and p.get("architecture") and p["architecture"] != "unknown":
            plats["%s/%s" % (p["os"], p["architecture"])] = m["digest"]
    if not plats:
        raise Failed("%s is not a multi-platform index" % ref)
    return digest, plats


def run(tools: Path, tool: str, *args, check=True, capture=True) -> subprocess.CompletedProcess:
    exe = tools / tool
    if not exe.is_file():
        raise Failed("%s not found in %s (scripts/fetch_tools.py)" % (tool, tools))
    env = dict(os.environ, SYFT_CHECK_FOR_APP_UPDATE="false", COSIGN_YES="true")
    p = subprocess.run([str(exe), *args], capture_output=capture, env=env, timeout=1800)
    if check and p.returncode:
        raise Failed("%s %s failed: %s" % (tool, args[0], (p.stderr or b"").decode("utf-8", "replace")[-600:]))
    return p


def ident_args(identity: str) -> list:
    return ["--certificate-identity", identity, "--certificate-oidc-issuer", ISSUER]


def signature_ok(tools, image_digest, identity) -> bool:
    return run(tools, "cosign", "verify", *ident_args(identity), "-o", "json", image_digest, check=False).returncode == 0


def attested_sboms(tools, image_digest, identity) -> dict:
    """{"<sbom name>": spdx document} of the valid SPDX attestations, {} when there is none."""
    p = run(tools, "cosign", "verify-attestation", "--type", "spdxjson", *ident_args(identity), image_digest, check=False)
    if p.returncode:
        return {}
    out = {}
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        env = json.loads(line)
        env = env.get("dsseEnvelope") or env
        if "payload" not in env:
            continue
        stmt = json.loads(base64.b64decode(env["payload"]))
        pred = stmt.get("predicate") or {}
        if isinstance(pred, dict) and str(pred.get("spdxVersion", "")).startswith("SPDX-"):
            out[pred.get("name", "?")] = pred
    return out


def plat_short(platform: str) -> str:
    """"<os>/amd64" -> "amd64" (the image is built for one operating system)."""
    return platform.rsplit("/", 1)[-1]


def sbom_name(platform: str) -> str:
    return "lanloom-" + plat_short(platform)


def cmd_publish(a) -> None:
    image = split_ref(a.image)
    base = "%s/%s" % image[:2]
    index, plats = registry_index(a.image)
    target = "%s@%s" % (base, index)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if signature_ok(a.tools, target, a.identity):
        print("signature: present for %s" % index)
    else:
        run(a.tools, "cosign", "sign", target, capture=False)
        print("signature: created for %s" % index)
    have = attested_sboms(a.tools, target, a.identity)
    for plat, pdigest in sorted(plats.items()):
        name = sbom_name(plat)
        path = out / ("lanloom-%s-%s.spdx.json" % (a.version, plat_short(plat)))
        if name in have:
            path.write_text(json.dumps(have[name], indent=1))
            print("sbom %s: attestation present" % plat)
            continue
        run(a.tools, "syft", "registry:%s@%s" % (base, pdigest), "--source-name", name, "--source-version", a.version,
            "-o", "spdx-json=%s" % path, "-q")
        doc = json.loads(path.read_text())
        if len(doc.get("packages") or []) < 50:
            raise Failed("SBOM of %s has only %d packages" % (plat, len(doc.get("packages") or [])))
        run(a.tools, "cosign", "attest", "--type", "spdxjson", "--predicate", str(path), target, capture=False)
        print("sbom %s: %d packages, attestation created" % (plat, len(doc["packages"])))
    verify(a.tools, a.image, a.identity)


def verify(tools, ref, identity) -> dict:
    image = split_ref(ref)
    index, plats = registry_index(ref)
    target = "%s/%s@%s" % (image[0], image[1], index)
    problems = []
    if not signature_ok(tools, target, identity):
        problems.append("no valid signature from %s" % identity)
    sboms = attested_sboms(tools, target, identity)
    for plat in sorted(plats):
        if sbom_name(plat) not in sboms:
            problems.append("no signed SBOM attestation for %s" % plat)
    print("image %s\n  index %s\n  platforms %s\n  signature %s\n  sboms %s" % (
        ref, index, ", ".join(sorted(plats)), "ok" if not problems or "signature" not in problems[0] else "MISSING",
        ", ".join("%s (%d packages)" % (n, len(d.get("packages") or [])) for n, d in sorted(sboms.items())) or "none"))
    if problems:
        raise Failed("; ".join(problems))
    return {"index": index, "platforms": plats, "sboms": sboms}


def cmd_verify(a) -> None:
    verify(a.tools, a.image, a.identity)
    print("VERIFIED: signed by %s" % a.identity)


def cmd_sboms(a) -> None:
    res = verify(a.tools, a.image, a.identity)
    version = split_ref(a.image)[2]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sums = []
    for plat in sorted(res["platforms"]):
        path = out / ("lanloom-%s-%s.spdx.json" % (version, plat_short(plat)))
        data = json.dumps(res["sboms"][sbom_name(plat)], indent=1).encode()
        path.write_bytes(data)
        sums.append("%s  %s" % (hashlib.sha256(data).hexdigest(), path.name))
    (out / ("lanloom-%s-sbom.sha256" % version)).write_text("\n".join(sums) + "\n")
    print("\n".join(sums))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("publish", "verify", "sboms"):
        p = sub.add_parser(name)
        p.add_argument("image")
        p.add_argument("--tools", type=Path, required=True)
        p.add_argument("--identity", default=IDENTITY)
        if name == "publish":
            p.add_argument("--version", required=True)
        if name in ("publish", "sboms"):
            p.add_argument("--out", default="sbom")
    a = ap.parse_args(argv)
    try:
        {"publish": cmd_publish, "verify": cmd_verify, "sboms": cmd_sboms}[a.cmd](a)
    except Failed as exc:
        print("sign_image: %s" % exc, file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("sign_image: tool failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
