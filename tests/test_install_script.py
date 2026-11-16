"""Dry run of deploy/install.sh with a fake docker on PATH."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "install.sh"

FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "exec lanloom")
    case "$*" in
      *lanloom.ai.runtime*) cd "$REPO" && exec "$PY" -m lanloom.ai.runtime compose ;;
      *) exit 0 ;;
    esac ;;
  "logs --tail") exit 0 ;;
esac
case "$1" in
  logs) exit 0 ;;
  inspect) echo "running 0" ;;
esac
exit 0
"""


@pytest.fixture()
def sandbox(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_LOG=str(log), REPO=str(ROOT),
               PY=sys.executable, HOME=str(tmp_path), PYTHONPATH=str(ROOT))
    return tmp_path, env, log


def run(sandbox, *args):
    tmp, env, _ = sandbox
    return subprocess.run(["sh", str(SCRIPT), "--dir", str(tmp / "lanloom"), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_syntax():
    assert subprocess.run(["sh", "-n", str(SCRIPT)]).returncode == 0


def test_without_ai(sandbox):
    tmp, _, log = sandbox
    res = run(sandbox)
    assert res.returncode == 0, res.stderr
    assert not (tmp / "lanloom" / "compose.override.yaml").exists()
    assert "Local AI: add it later in Settings > AI" in res.stdout


def test_with_ai_writes_override_once(sandbox):
    tmp, _, log = sandbox
    res = run(sandbox, "--with-ai")
    assert res.returncode == 0, res.stderr
    override = tmp / "lanloom" / "compose.override.yaml"
    assert "container_name: lanloom-ai" in override.read_text()
    assert "Local AI service: running - open Settings > AI" in res.stdout
    calls = log.read_text().splitlines()
    ups = [i for i, c in enumerate(calls) if c.endswith("up -d")]
    assert len(ups) == 2 and ups[1] > next(i for i, c in enumerate(calls) if "lanloom.ai.runtime compose" in c)
    assert not [p for p in (tmp / "lanloom").iterdir() if p.name.startswith(".compose.override")]
    # second run: nothing is overwritten
    override.write_text(override.read_text() + "# keep me\n")
    res = run(sandbox, "--with-ai")
    assert res.returncode == 0, res.stderr
    assert "# keep me" in override.read_text()


def test_foreign_override_is_left_alone(sandbox):
    tmp, _, _ = sandbox
    (tmp / "lanloom").mkdir()
    (tmp / "lanloom" / "compose.override.yaml").write_text("services: {}\n")
    res = run(sandbox, "--with-ai")
    assert res.returncode == 0, res.stderr
    assert (tmp / "lanloom" / "compose.override.yaml").read_text() == "services: {}\n"
    assert "not touching it" in res.stderr
    assert "Local AI: add it later" in res.stdout


def test_failed_snippet_leaves_no_file(sandbox):
    tmp, env, _ = sandbox
    env["PY"] = "false"
    res = run((tmp, env, None), "--with-ai")
    assert res.returncode != 0
    assert not (tmp / "lanloom" / "compose.override.yaml").exists()
    assert not [p for p in (tmp / "lanloom").iterdir() if p.name.startswith(".compose.override")]
