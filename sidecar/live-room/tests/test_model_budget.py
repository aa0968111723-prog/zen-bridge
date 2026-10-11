import json
from pathlib import Path

import pytest

from tools import model_budget


HW = {
    "ram_available_gb": 4.8,
    "logical_cores": 12,
    "vulkan_devices": ["AMD Radeon"],
    "on_ac_power": True,
    "power_plan": "Balanced",
}
WORKERS = {
    "workers": [
        {
            "name": "final_asr",
            "backend": "vulkan",
            "threads": 4,
            "model_ram_gb": 1.0,
            "extra_ram_gb": 0.2,
        },
        {
            "name": "draft_asr",
            "backend": "cpu",
            "threads": 1,
            "model_ram_gb": 0.5,
            "extra_ram_gb": 0.1,
        },
        {
            "name": "mt",
            "backend": "cpu",
            "threads": 2,
            "model_ram_gb": 1.5,
            "extra_ram_gb": 0.3,
        },
    ]
}


def test_recommended_example_config_has_expected_workers():
    path = Path(__file__).parent / "data" / "model_budget.example.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    workers = config["workers"]

    assert [
        (worker["name"], worker["backend"], worker["threads"]) for worker in workers
    ] == [
        ("Breeze ASR (final)", "vulkan", 4),
        ("X-ASR int8 (draft)", "cpu", 1),
        ("Hy-MT2 GGUF Q4", "cpu", 2),
    ]

    assert model_budget.check_budget(HW, config)["fits"]
    assert not model_budget.check_budget(HW | {"ram_available_gb": 2.0}, config)["fits"]


def test_fits_with_headroom_and_cpu_budget():
    report = model_budget.check_budget(HW, WORKERS)

    assert report["fits"]
    assert report["ram"]["required_gb"] == pytest.approx(4.6)
    assert report["ram"]["available_gb"] == 4.8
    assert report["ram"]["headroom_gb"] == 1.0
    assert report["cpu"] == {"requested_threads": 3, "available_threads": 11}


def test_ram_over_budget_fails():
    report = model_budget.check_budget(HW, WORKERS | {"workers": [
        WORKERS["workers"][0] | {"model_ram_gb": 4.0},
        *WORKERS["workers"][1:],
    ]})

    assert not report["fits"]
    assert "RAM requirement exceeds available RAM" in report["errors"]


def test_cpu_over_budget_fails_and_reserves_one_logical_core():
    hardware = HW | {"logical_cores": 4}
    config = {"workers": [worker | {"backend": "cpu", "threads": 2} for worker in WORKERS["workers"]]}
    report = model_budget.check_budget(hardware, config)

    assert not report["fits"]
    assert report["cpu"]["available_threads"] == 3
    assert "CPU worker threads exceed the logical-core budget" in report["errors"]


def test_concurrent_high_cpu_threads_warn():
    config = {
        "workers": [
            {"name": "final-asr", "backend": "cpu", "threads": 4, "model_ram_gb": 0, "extra_ram_gb": 0},
            {"name": "MT", "backend": "cpu", "threads": 4, "model_ram_gb": 0, "extra_ram_gb": 0},
        ]
    }
    report = model_budget.check_budget(HW, config)

    assert any("Final ASR and MT" in warning for warning in report["warnings"])


def test_missing_vulkan_device_warns():
    report = model_budget.check_budget(HW | {"vulkan_devices": []}, WORKERS)

    assert report["fits"]
    assert any("no Vulkan device" in warning for warning in report["warnings"])


def test_battery_and_power_saver_warn():
    report = model_budget.check_budget(
        HW | {"on_ac_power": False, "power_plan": "Power Saver"}, WORKERS
    )

    assert any("running on battery" in warning for warning in report["warnings"])
    assert any("power saver" in warning for warning in report["warnings"])


@pytest.mark.parametrize(
    "hardware,config",
    [
        ([], WORKERS),
        (HW, []),
        (HW | {"ram_available_gb": None}, WORKERS),
        (HW | {"logical_cores": True}, WORKERS),
        (HW, {"workers": [{}]}),
        (HW, {"workers": [WORKERS["workers"][0] | {"backend": "gpu"}]}),
        (HW, {"workers": [WORKERS["workers"][0] | {"threads": 0}]}),
        (HW, {"workers": [WORKERS["workers"][0] | {"model_ram_gb": -1}]}),
        (HW, {"workers": [WORKERS["workers"][0], WORKERS["workers"][0]]}),
    ],
)
def test_bad_input_raises_budget_input_error(hardware, config):
    with pytest.raises(model_budget.BudgetInputError):
        model_budget.check_budget(hardware, config)


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def test_cli_outputs_summary_and_json(tmp_path, capsys):
    hw_path = _write_json(tmp_path / "hw.json", HW)
    config_path = _write_json(tmp_path / "config.json", WORKERS)
    report_path = tmp_path / "report.json"

    result = model_budget.main(
        ["--hw", hw_path, "--config", config_path, "--json", str(report_path)]
    )

    assert result == 0
    assert "Model budget: FITS" in capsys.readouterr().out
    assert json.loads(report_path.read_text(encoding="utf-8"))["fits"]


def test_cli_returns_one_when_configuration_does_not_fit(tmp_path, capsys):
    hw_path = _write_json(tmp_path / "hw.json", HW | {"ram_available_gb": 1.0})
    config_path = _write_json(tmp_path / "config.json", WORKERS)

    result = model_budget.main(["--hw", hw_path, "--config", config_path])

    assert result == 1
    assert "Model budget: DOES NOT FIT" in capsys.readouterr().out


def test_cli_returns_two_for_invalid_json(tmp_path, capsys):
    hw_path = tmp_path / "hw.json"
    hw_path.write_text("{", encoding="utf-8")
    config_path = _write_json(tmp_path / "config.json", WORKERS)

    assert model_budget.main(["--hw", str(hw_path), "--config", config_path]) == 2
    assert "invalid JSON" in capsys.readouterr().err


@pytest.mark.parametrize("field", ["ram_available_gb", "logical_cores"])
def test_cli_returns_two_with_clear_message_for_null_probe_field(tmp_path, capsys, field):
    hardware = HW | {field: None}
    hw_path = _write_json(tmp_path / "hw.json", hardware)
    config_path = _write_json(tmp_path / "config.json", WORKERS)

    result = model_budget.main(["--hw", hw_path, "--config", config_path])

    assert result == 2
    assert f"hardware.{field}" in capsys.readouterr().err


def test_cli_refuses_to_overwrite_json(tmp_path, capsys):
    hw_path = _write_json(tmp_path / "hw.json", HW)
    config_path = _write_json(tmp_path / "config.json", WORKERS)
    report_path = tmp_path / "report.json"
    report_path.write_text("keep me", encoding="utf-8")

    result = model_budget.main(
        ["--hw", hw_path, "--config", config_path, "--json", str(report_path)]
    )

    assert result == 2
    assert "File exists" in capsys.readouterr().err
    assert report_path.read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize("value", [10**400, -(10**400)])
@pytest.mark.parametrize("field", ["ram_available_gb", "model_ram_gb", "extra_ram_gb"])
def test_cli_rejects_huge_json_integers(tmp_path, capsys, field, value):
    hardware = HW | {field: value} if field == "ram_available_gb" else HW
    config = WORKERS if field == "ram_available_gb" else {
        "workers": [WORKERS["workers"][0] | {field: value}]
    }
    hw_path = _write_json(tmp_path / "hw.json", hardware)
    config_path = _write_json(tmp_path / "config.json", config)

    with pytest.raises(model_budget.BudgetInputError, match=field):
        model_budget.check_budget(hardware, config)
    assert model_budget.main(["--hw", hw_path, "--config", config_path]) == 2
    error = capsys.readouterr().err
    assert field in error
    assert "finite and non-negative" in error
    assert "Traceback" not in error


@pytest.mark.parametrize(
    "names", [("Whisper final", "translator"), ("最終辨識", "翻譯")]
)
def test_explicit_roles_warn_independently_of_display_names(names):
    config = {
        "workers": [
            WORKERS["workers"][0] | {
                "name": names[0], "role": "final_asr", "backend": "cpu", "threads": 4,
            },
            WORKERS["workers"][2] | {
                "name": names[1], "role": "mt", "threads": 4,
            },
        ]
    }

    report = model_budget.check_budget(HW, config)

    assert any("Final ASR and MT" in warning for warning in report["warnings"])


def test_explicit_draft_role_overrides_final_asr_name():
    config = {
        "workers": [
            WORKERS["workers"][0] | {
                "role": "draft_asr", "backend": "cpu", "threads": 4,
            },
            WORKERS["workers"][2] | {"role": "mt", "threads": 4},
        ]
    }

    report = model_budget.check_budget(HW, config)

    assert not any("Final ASR and MT" in warning for warning in report["warnings"])


def test_high_thread_worker_not_hidden_by_first_worker_of_same_role():
    config = {
        "workers": [
            WORKERS["workers"][0] | {"role": "final_asr"},
            WORKERS["workers"][0] | {
                "name": "Whisper final", "role": "final_asr",
                "backend": "cpu", "threads": 4,
            },
            WORKERS["workers"][2] | {"role": "mt", "threads": 4},
        ]
    }

    report = model_budget.check_budget(HW, config)

    assert any("Final ASR and MT" in warning for warning in report["warnings"])


@pytest.mark.parametrize("role", [None, "", "translator", 1, [], {}])
def test_invalid_worker_role_rejected(role):
    config = {"workers": [WORKERS["workers"][0] | {"role": role}]}

    with pytest.raises(model_budget.BudgetInputError, match=r"workers\[0\].role"):
        model_budget.check_budget(HW, config)


@pytest.mark.parametrize(
    "guid,name,expected",
    [
        ("A1841308-3541-4FAB-BC81-F71556F20B4A", "Économie d’énergie", True),
        ("381b4222-f694-41f0-9685-ff5bb260df2e", "Power Saver", False),
        (None, "a1841308-3541-4fab-bc81-f71556f20b4a", True),
    ],
)
def test_power_plan_guid_takes_precedence_over_display_name(guid, name, expected):
    report = model_budget.check_budget(
        HW | {"power_plan_guid": guid, "power_plan": name}, WORKERS
    )

    assert any("power saver" in warning for warning in report["warnings"]) is expected


def test_invalid_power_plan_guid_rejected():
    with pytest.raises(model_budget.BudgetInputError, match="hardware.power_plan_guid"):
        model_budget.check_budget(HW | {"power_plan_guid": 1}, WORKERS)


def test_example_config_declares_worker_roles():
    path = Path(__file__).parent / "data" / "model_budget.example.json"
    config = json.loads(path.read_text(encoding="utf-8"))

    assert [worker["role"] for worker in config["workers"]] == [
        "final_asr", "draft_asr", "mt",
    ]
