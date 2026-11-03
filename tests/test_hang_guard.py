"""The hang guard in conftest.py ends a stuck run quickly, with the stacks of all threads."""
import shutil
import subprocess
import sys
import time
from pathlib import Path


def test_hanging_test_ends_the_run_with_stacks(tmp_path):
    shutil.copy(Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / "test_stuck.py").write_text(
        "import threading, time\n"
        "def test_stuck():\n"
        "    threading.Event().wait()\n")
    env = {"PATH": "/usr/bin:/bin", "LANLOOM_TEST_HANG_S": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
                           str(tmp_path / "test_stuck.py")], cwd=tmp_path, env=env, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert time.monotonic() - started < 30
    assert "test hangs for more than 1 s: test_stuck.py::test_stuck" in proc.stderr
    assert "test_stuck.py" in proc.stderr and "Thread 0x" in proc.stderr   # faulthandler stacks of all threads

