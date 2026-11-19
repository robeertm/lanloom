"""The hang guard in conftest.py ends a stuck run quickly, with the stacks of all threads."""
import shutil
import subprocess
import sys
from pathlib import Path


def test_hanging_test_ends_the_run_with_stacks(tmp_path):
    shutil.copy(Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / "test_stuck.py").write_text(
        "import threading, time\n"
        "def test_stuck():\n"
        "    threading.Event().wait()\n")
    env = {"PATH": "/usr/bin:/bin", "LANLOOM_TEST_HANG_S": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
                           str(tmp_path / "test_stuck.py")], cwd=tmp_path, env=env, capture_output=True, text=True,
                          timeout=120)     # the guard fires after 1 s; this only stops a broken guard
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "test hangs for more than 1 s: test_stuck.py::test_stuck" in proc.stderr
    assert "test_stuck.py" in proc.stderr and "Thread 0x" in proc.stderr   # faulthandler stacks of all threads



def test_a_long_test_that_reports_progress_is_not_a_hang(tmp_path):
    """Progress, not the clock: a test that works for three times the limit but reports each step passes; one that reports a
    step and then gets stuck still ends the run, with its stacks."""
    shutil.copy(Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / "test_long.py").write_text(
        "import threading, time\n"
        "def test_long_but_working(test_progress):\n"
        "    for _ in range(12):\n"
        "        time.sleep(0.25)\n"
        "        test_progress()\n"
        "def test_progress_then_stuck(test_progress):\n"
        "    test_progress()\n"
        "    threading.Event().wait()\n")
    env = {"PATH": "/usr/bin:/bin", "LANLOOM_TEST_HANG_S": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
                           str(tmp_path / "test_long.py")], cwd=tmp_path, env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "test hangs for more than 1 s: test_long.py::test_progress_then_stuck" in proc.stderr
    assert "test_long_but_working" not in proc.stderr
    assert proc.stdout.startswith(".")                                  # the long test that reported progress passed
