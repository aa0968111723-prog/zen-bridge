"""Pure hardware recommendations never apply runtime configuration."""
from copy import deepcopy
import os

import pytest

from app.autotune import suggest


@pytest.fixture
def ryzen():
    return {
        "physical_cores": 6,
        "logical_cores": 12,
        "cpu_flags": {"avx2": True},
        "ram_total_gb": 15.4,
        "ram_available_gb": 8,
        "on_ac_power": True,
        "vulkan_devices": [{"name": "Radeon iGPU"}],
    }


@pytest.mark.parametrize("lang,quantisation", [("en", "Q4_K_M"), ("ja", "Q8_0")])
def test_5600h(ryzen, lang, quantisation):
    result = suggest(ryzen, lang)
    assert 4 <= int(result["BREEZE_ASR_THREADS"]) <= 5
    assert 1 <= int(result["BREEZE_TRANSLATE_NUM_THREAD"]) <= 2
    assert result["BREEZE_DRAFT_THREADS"] == "1"
    assert result["ZEN_EMBED"] == "0"
    assert result["proposed_new_keys"] == {
        "BREEZE_TRANSLATE_QUANTIZATION": quantisation,
        "BREEZE_ASR_BACKEND": "vulkan",
    }
    assert result["reasons"] and not result["warnings"]


@pytest.mark.parametrize("available,quantisation", [(5.99, "Q4_K_M"), (6, "Q8_0")])
def test_four_core_ram_boundary(ryzen, available, quantisation):
    ryzen.update(physical_cores=4, ram_available_gb=available)
    result = suggest(ryzen, "ja")
    assert result["proposed_new_keys"]["BREEZE_TRANSLATE_QUANTIZATION"] == quantisation
    assert bool(result["warnings"]) is (available < 6)
    if available < 6:
        assert "6 GB" in result["warnings"][0]


@pytest.mark.parametrize("devices", [[], None])
def test_no_vulkan(ryzen, devices):
    ryzen["vulkan_devices"] = devices
    assert suggest(ryzen, "en")["proposed_new_keys"]["BREEZE_ASR_BACKEND"] == "cpu"


@pytest.mark.parametrize("devices", ["Radeon", {"name": "Radeon"}, True, 1, ("Radeon",)])
def test_non_list_vulkan_is_not_evidence_of_devices(ryzen, devices):
    ryzen["vulkan_devices"] = devices
    assert suggest(ryzen, "en")["proposed_new_keys"]["BREEZE_ASR_BACKEND"] == "cpu"


@pytest.mark.parametrize("ram", [None, "8", {}, [], True, float("nan"), float("inf")])
def test_unknown_ram_uses_conservative_quantisation(ryzen, ram):
    ryzen["ram_available_gb"] = ram
    result = suggest(ryzen, "ja")
    assert result["proposed_new_keys"]["BREEZE_TRANSLATE_QUANTIZATION"] == "Q4_K_M"
    assert any("6 GB" in warning for warning in result["warnings"])


def test_hw_probe_unknown_value_output():
    hw = {
        "os": "Windows",
        "python": "3.12.0",
        "cpu_model": "AMD Ryzen 5 5600H",
        "physical_cores": 6,
        "logical_cores": 12,
        "cpu_flags": {
            "avx": None, "avx2": None, "fma": None, "f16c": None,
            "avx512f": None, "avx512_vnni": None, "avx_vnni": None,
        },
        "ram_total_gb": None,
        "ram_available_gb": None,
        "on_ac_power": None,
        "battery_percent": None,
        "power_plan": None,
        "vulkan_devices": [],
        "notes": ["RAM: OSError: RAM unavailable"],
    }
    original = deepcopy(hw)
    result = suggest(hw, "ja")
    assert result["proposed_new_keys"] == {
        "BREEZE_TRANSLATE_QUANTIZATION": "Q4_K_M",
        "BREEZE_ASR_BACKEND": "cpu",
    }
    assert any("6 GB" in warning for warning in result["warnings"])
    assert any("Power source is unknown" in warning for warning in result["warnings"])
    assert hw == original


@pytest.mark.parametrize("ac,override", [(False, None), (True, True), (False, True)])
def test_battery_lowers_threads_and_warns(ryzen, ac, override):
    plugged = suggest(ryzen, "en")
    ryzen["on_ac_power"] = ac
    result = suggest(ryzen, "en", on_battery=override)
    assert int(result["BREEZE_ASR_THREADS"]) < int(plugged["BREEZE_ASR_THREADS"])
    assert int(result["BREEZE_TRANSLATE_NUM_THREAD"]) <= int(plugged["BREEZE_TRANSLATE_NUM_THREAD"])
    assert any("latency measurements are not trustworthy" in w for w in result["warnings"])


def test_explicit_power_override(ryzen):
    plugged = suggest(ryzen, "en")
    ryzen["on_ac_power"] = False
    assert suggest(ryzen, "en", on_battery=False) == plugged


@pytest.mark.parametrize("lang", ["zh", "fr", "", "EN", None])
def test_invalid_target(ryzen, lang):
    with pytest.raises(ValueError, match="target_lang"):
        suggest(ryzen, lang)


@pytest.mark.parametrize("cores", [2, 3, 4, 6, 8, 12, 16])
@pytest.mark.parametrize("battery", [False, True])
@pytest.mark.parametrize("lang", ["en", "ja"])
def test_thread_budget(ryzen, cores, battery, lang):
    ryzen.update(physical_cores=cores, logical_cores=cores * 2)
    result = suggest(ryzen, lang, on_battery=battery)
    asr = int(result["BREEZE_ASR_THREADS"])
    mt = int(result["BREEZE_TRANSLATE_NUM_THREAD"])
    assert asr >= 1 and mt >= 1
    assert asr + mt <= cores
    if cores >= 3:
        assert asr + mt + int(result["BREEZE_DRAFT_THREADS"]) <= cores
    else:
        assert any("serially" in w for w in result["warnings"])


@pytest.mark.parametrize("cores", [None, 0, 1, -1, True, 6.5, "6"])
def test_invalid_physical_cores(ryzen, cores):
    ryzen["physical_cores"] = cores
    with pytest.raises(ValueError, match="physical_cores"):
        suggest(ryzen, "en")


def test_pure_deterministic_and_isolated(ryzen, monkeypatch):
    original = deepcopy(ryzen)
    monkeypatch.setenv("BREEZE_ASR_THREADS", "99")
    monkeypatch.setenv("ZEN_EMBED", "1")
    environment = dict(os.environ)
    first = suggest(ryzen, "ja")
    assert first == suggest(ryzen, "ja")
    assert ryzen == original and dict(os.environ) == environment
    assert first["ZEN_EMBED"] == "0"
    assert set(first) == {
        "BREEZE_ASR_THREADS", "BREEZE_DRAFT_THREADS", "BREEZE_TRANSLATE_NUM_THREAD",
        "ZEN_EMBED", "proposed_new_keys", "reasons", "warnings",
    }
    assert all(isinstance(value, str) for key, value in first.items()
               if key not in ("reasons", "warnings", "proposed_new_keys"))
    assert all(isinstance(value, str) for value in first["reasons"] + first["warnings"])
    first["warnings"].append("caller mutation")
    assert suggest(ryzen, "ja")["warnings"] == []


def test_unknown_power_and_ram(ryzen):
    del ryzen["on_ac_power"]
    del ryzen["ram_available_gb"]
    result = suggest(ryzen, "ja")
    assert result["proposed_new_keys"]["BREEZE_TRANSLATE_QUANTIZATION"] == "Q4_K_M"
    assert any("Power source is unknown" in w for w in result["warnings"])
    assert any("6 GB" in w for w in result["warnings"])
