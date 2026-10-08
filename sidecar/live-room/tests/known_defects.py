"""Known defects for the headless simulation suite.

P0 items that the spec marked failing on main are fixed on this branch, so they
are not listed. An entry here is a strict xfail only while the defect is still
present. Do not add one to hide a failure this branch can fix.
"""

import pytest

KNOWN_DEFECTS: dict[str, tuple[str, bool]] = {}


def defect(kd: str):
    reason, strict = KNOWN_DEFECTS[kd]
    return pytest.mark.xfail(strict=strict, reason=f"{kd}: {reason}")
