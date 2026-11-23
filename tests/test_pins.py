"""The image installs exactly the pinned list (B8).

  * requirements.txt: every line pinned with ==, each package once, pip pinned on its own line;
  * the Dockerfile installs pip first from that pin, then the list with --no-deps, then runs `pip check`, and marks the
    image so the self-test compares installed and pinned packages;
  * the comparison itself: a missing pin, an extra package and a wrong version are each reported (red);
  * here, outside the image: every pinned package is installed with exactly its pinned version (extra test tools
    are normal here, so that direction is only checked in the image, by the self-test);
  * the self-test fails when the image is marked and the list does not match."""
import re
from pathlib import Path

import pytest

from lanloom import pins

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()


def test_requirements_are_complete_pins():
    want = pins.pinned()
    assert len(want) > 20
    assert list(want)[0] == "fastapi"
    assert "pip" in want and re.fullmatch(r"\d+(\.\d+)+", want["pip"])
    for name in ("uvicorn", "argon2-cffi", "webauthn", "argon2-cffi-bindings", "starlette", "pydantic-core"):
        assert name in want, name


@pytest.mark.parametrize("text, err", [("fastapi>=0.1\n", "not pinned"), ("fastapi\n", "not pinned"),
                                       ("a==1\nA==1\n", "twice"), ("pyyaml==1\nPyYAML==1\n", "twice"), ("# only a comment\n", "no requirements")])
def test_bad_lists_are_refused(tmp_path, text, err):
    p = tmp_path / "r.txt"
    p.write_text(text)
    with pytest.raises(pins.PinError, match=err):
        pins.pinned(p)


def test_extras_and_names_are_normalised(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text("uvicorn[standard]==0.54.0  # comment\npydantic_core==2.50.5\nPyYAML==6.0.4\n")
    assert pins.pinned(p) == {"uvicorn": "0.54.0", "pydantic-core": "2.50.5", "pyyaml": "6.0.4"}


def test_the_dockerfile_installs_exactly_the_list():
    flat = re.sub(r"\\\n\s*", " ", DOCKERFILE)
    run = next(line for line in flat.splitlines() if "requirements.txt" in line and line.startswith("RUN"))
    first_pip = run.index("grep -E '^pip==' requirements.txt")
    no_deps = run.index("--no-deps -r requirements.txt")
    check = run.index("pip check")
    assert first_pip < no_deps < check
    assert "pip install --no-cache-dir -r requirements.txt" not in flat           # the old way would resolve on its own
    assert len(re.findall(r"pip install[^&]*-r requirements\.txt", flat)) == 1
    assert re.search(r"^\s*LANLOOM_IMAGE=1 \\$", DOCKERFILE, re.M)


def test_every_difference_is_reported():
    want = {"fastapi": "0.165.0", "starlette": "1.10.0", "pip": "26.3"}
    assert pins.problems(want, dict(want)) == []
    missing = pins.problems(want, {"fastapi": "0.165.0", "pip": "26.3"})
    assert missing == ["pinned package missing: starlette"]
    extra = pins.problems(want, dict(want, sneaky="1.0"))             # a transitive package nobody pinned
    assert extra == ["package installed without a pin: sneaky 1.0"]
    wrong = pins.problems(want, dict(want, starlette="0.41.3"))
    assert wrong == ["installed version differs: starlette 0.41.3 (pinned 1.10.0)"]


def test_an_unreadable_list_is_a_problem(tmp_path, monkeypatch):
    monkeypatch.setattr(pins, "REQUIREMENTS", tmp_path / "missing.txt")
    assert pins.problems(have={})[0].startswith("package list unreadable")


def test_the_installed_list_matches_the_standard_library_view():
    """pins.installed() reads the installer's metadata folders itself (the package may not use the import machinery,
    see test_integrations.test_no_plugin_mechanism); here it is checked against importlib.metadata."""
    from importlib import metadata
    std = {}
    for dist in metadata.distributions():
        if dist.metadata and dist.metadata.get("Name"):
            std.setdefault(pins.norm(dist.metadata["Name"]), dist.version)
    assert pins.installed() == std


def test_metadata_folders_are_read_directly(tmp_path):
    site = tmp_path / "site"
    (site / "Foo_Bar-1.2.dist-info").mkdir(parents=True)
    (site / "Foo_Bar-1.2.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: Foo_Bar\nVersion: 1.2\n\nName: not-this\n")
    (site / "old.egg-info").mkdir()
    (site / "old.egg-info" / "PKG-INFO").write_text("Name: Old\nVersion: 0.9\n")
    (site / "broken-1.dist-info").mkdir()                            # no METADATA: not a distribution
    shadow = tmp_path / "shadow" / "foo-bar-9.dist-info"
    shadow.mkdir(parents=True)
    (shadow / "METADATA").write_text("Name: foo-bar\nVersion: 9\n")
    assert pins.installed([str(site), str(tmp_path / "missing")]) == {"foo-bar": "1.2", "old": "0.9"}
    assert pins.installed([str(site), str(tmp_path / "shadow")])["foo-bar"] == "1.2"     # first on the path wins


def test_the_pinned_versions_are_the_installed_ones_here():
    diff = pins.compare(pins.pinned(), pins.installed())
    assert diff["missing"] == [] and diff["wrong"] == [], diff


def test_selftest_fails_in_a_marked_image_with_an_unpinned_package(monkeypatch, capsys):
    from lanloom import main
    monkeypatch.setenv(pins.IMAGE_MARKER, "1")
    monkeypatch.setattr(pins, "setid_files", lambda: [])        # the test machine is not the image (see the test below)
    have = dict(pins.pinned(), sneaky="1.0")
    monkeypatch.setattr(pins, "installed", lambda: have)
    assert main.selftest() == 1
    assert "package installed without a pin: sneaky 1.0" in capsys.readouterr().out
    monkeypatch.setattr(pins, "installed", lambda: dict(pins.pinned()))
    assert main.selftest() == 0
    assert "selftest: ok" in capsys.readouterr().out


def test_selftest_fails_in_a_marked_image_with_a_set_user_id_file(monkeypatch, capsys):
    from lanloom import main
    monkeypatch.setenv(pins.IMAGE_MARKER, "1")
    monkeypatch.setattr(pins, "installed", lambda: dict(pins.pinned()))
    monkeypatch.setattr(pins, "setid_files", lambda: ["/usr/bin/mount"])
    assert main.selftest() == 1
    assert "set-user-ID or set-group-ID file in the image: /usr/bin/mount" in capsys.readouterr().out
    monkeypatch.delenv(pins.IMAGE_MARKER)
    assert main.selftest() == 0                                  # outside the image (development machine) not checked
    capsys.readouterr()


def test_selftest_checks_the_list_only_in_the_image(monkeypatch, capsys):
    from lanloom import main
    monkeypatch.delenv(pins.IMAGE_MARKER, raising=False)
    monkeypatch.setattr(pins, "installed", lambda: dict(pins.pinned(), pytest="8.3.4"))
    assert main.selftest() == 0
    capsys.readouterr()


def test_selftest_refuses_a_password_library_that_rewrites_old_hashes(monkeypatch, capsys):
    from argon2 import PasswordHasher
    from lanloom import auth, main
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=4))     # changed defaults: every stored hash looks outdated
    assert main.selftest() == 1
    assert "password hashes" in capsys.readouterr().out


def test_the_notices_list_exactly_the_pinned_packages():
    text = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    block = text.split("## Python packages", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| ([A-Za-z0-9._-]+) \| ([0-9][^ |]*) \| (.+?) \|$", block, re.M)
    listed = {pins.norm(n): v for n, v, _ in rows}
    assert len(listed) == len(rows)
    assert listed == pins.pinned()
    assert all(lic.strip() for _, _, lic in rows)


def test_setid_files_finds_set_user_id_and_set_group_id_files(tmp_path):
    import os
    import stat
    plain, suid, sgid = tmp_path / "plain", tmp_path / "suid", tmp_path / "sub" / "sgid"
    sgid.parent.mkdir()
    for f in (plain, suid, sgid):
        f.write_text("x")
    os.chmod(suid, 0o4755)
    os.chmod(sgid, 0o2755)
    (tmp_path / "link").symlink_to(suid)
    if not (os.stat(suid).st_mode & stat.S_ISUID):
        pytest.skip("file system does not keep set-user-ID bits")
    assert pins.setid_files([str(tmp_path)]) == sorted([str(suid), str(sgid)])
    os.chmod(suid, 0o755)
    os.chmod(sgid, 0o755)
    assert pins.setid_files([str(tmp_path)]) == []
    assert pins.setid_files([str(tmp_path / "missing")]) == []
