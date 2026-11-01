"""Helper for the board tests: run a Node script from tests/js and return its JSON output (skip without Node)."""
import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def run_node(script: str, timeout: int = 120):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the board engine is JavaScript and is tested with node")
    proc = subprocess.run([node, os.path.join(HERE, "js", script)], capture_output=True, text=True, timeout=timeout, check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)
