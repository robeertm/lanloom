"""Demo data and test fixtures hold no real-looking secrets (the repository is public).

The patterns describe the shape of secrets, not known values. A bait check proves each pattern still fires, so
the test cannot go quiet by accident. A line that holds a deliberate fake (a test that such input is refused)
carries the marker comment "secret-scan: fake"."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted([ROOT / "lanloom" / "demo.py", *(ROOT / "tests").glob("*.py"), *(ROOT / "tests" / "js").glob("*.js")])

UUID = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "hosted git token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "cloud access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "chat token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "signed web token": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."),
    "password in URL": re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://[^\s/:@'\"]+:[^\s/@'\"{}]+@"),
}
RUN = re.compile(r"[A-Za-z0-9_+/-]{32,}")
MARKER = "secret-scan: fake"


def random_runs(text: str) -> list:
    """32+ token characters with letters and digits. UUIDs, pure hex digests, paths and snake_case or kebab-case
    names (no piece of 16+ characters between "_" / "-") are identifiers, not secrets."""
    out = []
    for m in RUN.finditer(text):
        run = m.group(0)
        if UUID.match(run) or re.fullmatch(r"[0-9a-fA-F]+", run) or "/" in run:
            continue
        if max(len(piece) for piece in re.split(r"[_-]", run)) < 16:
            continue
        if re.search(r"[A-Za-z]", run) and re.search(r"\d", run):
            out.append(run)
    return out


def findings(text: str) -> list:
    hits = [name for name, rx in PATTERNS.items() if rx.search(text)]
    if random_runs(text):
        hits.append("random token")
    return hits


def test_bait_is_found():
    # assembled at run time: the bait itself must not sit in the repository as a literal
    bait = {
        "private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
        "hosted git token": "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
        "cloud access key": "AK" + "IA" + "ABCDEFGH12345678",
        "chat token": "xo" + "xb-" + "1234567890-abcdef",
        "bot token": "123456789" + ":" + "A" * 20 + "b" * 15,
        "signed web token": "ey" + "JhbGciOiJIUzI1NiJ9" + ".ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0" + ".x",
        "password in URL": "mqtt://user" + ":" + "pw" + "@broker",
        "random token": "Zq8Xw2Lp9Vr4Tn7B" + "m1Hc6Kd3Fs5Gj0Ya2Ue",
    }
    for name, text in bait.items():
        assert name in findings(text), name


def test_harmless_strings_pass():
    for text in ("uuid:2f402f80-da50-11e1-9b23-001788102201", "sha256:" + "ab" * 32, "http://192.0.2.1:49000/igd.xml",
                 "Living-Room-TV-4K", "urn:schemas-upnp-org:device:MediaRenderer:1",
                 "test_key_localization_sha256_matches_rfc7860_construction"):
        assert findings(text) == [], text


def test_fixtures_and_demo_data_hold_no_secrets():
    assert len(FILES) > 20
    bad = {}
    for path in FILES:
        text = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if MARKER not in line)
        hits = findings(text)
        if hits:
            bad[str(path.relative_to(ROOT))] = hits
    assert bad == {}
