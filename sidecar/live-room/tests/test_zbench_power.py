"""D-006: the benchmark must fail closed when the power source cannot be confirmed."""
import importlib.util
from pathlib import Path

ZB = Path(__file__).resolve().parents[1] / "scripts" / "bench" / "zbench.py"


def _load():
    spec = importlib.util.spec_from_file_location("zbench_power", ZB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Batt:
    def __init__(self, plugged):
        self.power_plugged = plugged


class _Ps:
    def __init__(self, value=None, boom=False):
        self.value, self.boom = value, boom

    def sensors_battery(self):
        if self.boom:
            raise OSError("no power api")
        return self.value


def test_psutil_reports_battery_and_ac():
    zb = _load()
    assert zb.on_battery(_Ps(_Batt(False))) is True
    assert zb.on_battery(_Ps(_Batt(True))) is False
    assert zb.on_battery(_Ps(None)) is False          # no battery at all = desktop on AC


def test_without_psutil_uses_ac_line_status():
    zb = _load()
    zb.psutil = None
    assert zb.on_battery(None, ac_status=0) is True
    assert zb.on_battery(None, ac_status=1) is False
    assert zb.on_battery(None, ac_status=255) is True  # unknown -> battery (fail closed)


def test_psutil_error_falls_back_to_ac_line_status():
    zb = _load()
    assert zb.on_battery(_Ps(boom=True), ac_status=0) is True
    assert zb.on_battery(_Ps(boom=True), ac_status=1) is False
