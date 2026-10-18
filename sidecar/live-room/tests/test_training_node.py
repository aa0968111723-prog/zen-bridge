"""Runs tests/training_logic.test.mjs (studio resample / WAV encode / level checks). Skips without node."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_training_logic_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    p = subprocess.run([node, str(ROOT / "tests" / "training_logic.test.mjs")], cwd=ROOT, capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
