"""The repository holds no secret (scripts/secret_scan.py over every tracked file), and the scanner still fires.

The history scan (--history) runs in CI and in the release gate; here only the tracked files are read. Without git
(a test image that holds an exported tree) the repository checks are skipped; CI and the release gate run them."""
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("secret_scan", ROOT / "scripts" / "secret_scan.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


HAS_GIT = shutil.which("git") is not None


def test_tracked_files_hold_no_secret():
    if not HAS_GIT or subprocess.run(["git", "-C", str(ROOT), "rev-parse"], capture_output=True).returncode:
        pytest.skip("not a git checkout")
    found = scan.scan_tree(ROOT)
    assert not found, ["%s:%d %s" % f for f in found]


# assembled at run time: the bait itself must not sit in the repository as a literal
BAIT = {
    "private key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "hosted git token": "gh" + "p_" + "Zq8Xw2Lp9Vr4Tn7Bm1Hc6Kd3Fs5Gj0Ya2Ue4",   # secret-scan: fake
    "cloud access key": "AK" + "IA" + "QWERTYUIOPASDFGH",
    "ai service key": "s" + "k-" + "Zq8Xw2Lp9Vr4Tn7Bm1Hc6Kd3Fs5Gj0Ya2Ue4Rt",   # secret-scan: fake
    "signed web token": "ey" + "JhbGciOiJIUzI1NiJ9" + ".ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0" + ".Sfl" + "KxwRJSMeKKF2QT4fw",   # secret-scan: fake
    "password in URL": "mqtt://watcher" + ":" + "Gq7rT2vX9" + "@broker.lan",   # secret-scan: fake
    "secret assignment": "api_key = '" + "Zq8Xw2Lp9Vr4Tn7Bm1Hc6Kd3Fs" + "'",   # secret-scan: fake
}


def test_bait_is_found_and_values_are_not_printed(capsys, tmp_path):
    for kind, text in BAIT.items():
        assert kind in [k for _, _, k in scan.scan_text(text, "bait")], kind
    assert not scan.scan_text(BAIT["hosted git token"] + "  # " + scan.MARKER, "bait")
    if not HAS_GIT:
        return
    # a whole repository with one secret: red, and the value never shows up in the output
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "config.py").write_text("TOKEN = None\nkey = %r\n" % BAIT["hosted git token"])
    subprocess.run(["git", "-C", str(tmp_path), "add", "config.py"], check=True)
    assert scan.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "config.py:2" in out and BAIT["hosted git token"] not in out


def test_placeholders_are_not_secrets():
    for text in ("url = 'mqtt://user:password@host'", "password = 'your-password-goes-here-123456'",   # secret-scan: fake
                 "token: '0123456789abcdef0123456789abcdef'", "see `scheme://user:password@host`"):   # secret-scan: fake
        assert not scan.scan_text(text, "doc"), text


def test_reviewed_history_lines_are_kept_as_hashes_only():
    import json
    data = json.loads((ROOT / scan.BASELINE).read_text())
    for e in data["reviewed"]:
        assert set(e) <= {"sha256", "file", "reason"} and len(e["sha256"]) == 64 and e["reason"].strip()
