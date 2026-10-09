"""Run the section B browser suites.

The workflow invokes the older .mjs files by name. These files are launched from
pytest so that change is not required. Skip only when node is missing or older
than 20 (this box is 20; CI is 22).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    "recorder_long.test.mjs",
    "room_client_restart.test.mjs",
    "room_client_room_end.test.mjs",
    "room_client_reset.test.mjs",
    "room_client_long.test.mjs",
    "host_caption.test.mjs",
)


def _node_bin() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, "-p", "process.versions.node"], capture_output=True, text=True, check=False)
    raw = (proc.stdout or "").strip()
    try:
        major = int(raw.split(".", 1)[0])
    except ValueError:
        pytest.skip(f"could not parse node version {raw!r}")
    if major < 20:
        pytest.skip(f"node {raw} is older than 20")
    return node


@pytest.fixture(scope="module")
def node_bin():
    return _node_bin()


@pytest.mark.parametrize("name", SUITES)
def test_node_suite(name, node_bin):
    """R1–R4, J1–J5, and B-b6. The P0 client defects are fixed on this branch, so none of these xfail."""
    proc = subprocess.run(
        [node_bin, str(ROOT / "tests" / name)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
