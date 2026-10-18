"""training/ scripts: configs parse, export is readable by them, and the CPU --dry-run trains a tiny model.

The dry-run needs torch + transformers + peft. CI without them only runs the config/data checks; set
ZEN_TRAIN_PYTHON to an interpreter that has them (e.g. a separate training venv) to run the full dry-run.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.training import dataset, harvest, registry, review
from app.training import db as tdb
from tests.training_fakes import class_clip, speechy, wav_bytes

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "training" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import common  # noqa: E402


def _py():
    py = os.environ.get("ZEN_TRAIN_PYTHON") or sys.executable
    ok = subprocess.run([py, "-c", "import torch, transformers, peft, accelerate"], capture_output=True).returncode == 0
    return py if ok else None


@pytest.fixture(scope="module")
def export(tmp_path_factory):
    root = tmp_path_factory.mktemp("tr")
    c = tdb.connect(root)
    try:
        for i in range(3):
            s = registry.new_speaker(c, f"講者{i}")["id"]
            registry.record_consent(c, s, scope_asr=True, scope_mt=True, harvest_live=True, recorded_by="社長",
                                    confirm_text=True)
            for sess in range(3):
                for seq in range(2):
                    w = root / f"{i}{sess}{seq}.wav"
                    w.write_bytes(wav_bytes(speechy(1.6, seed=i * 9 + sess * 3 + seq)))
                    t = f"第{i}位第{sess}場第{seq}句禪修"
                    cid = class_clip(c, root, room_id=f"room{i}", session_id=f"s{sess}", seq=seq, wav_path=w, zh_raw=t,
                                     zh=t, tgt_lang="en", speaker_id=s)
                    review.decide(c, root, cid, action="approve", actor="t", mt_text=f"Zen line {i}-{sess}-{seq}")
        ex = dataset.build(c, root)
    finally:
        c.close()
    return Path(ex["dir"])


def test_configs_parse_without_pyyaml():
    for name in ("asr_breeze_lora.yaml", "mt_qwen_lora.yaml"):
        cfg = common.load_config(REPO / "training" / "configs" / name)
        assert cfg["lora"]["r"] > 0 and isinstance(cfg["lora"]["target_modules"], list)
        assert cfg["train"]["learning_rate"] > 0 and "dry_run" in cfg
    assert common.load_config(REPO / "training" / "configs" / "asr_breeze_lora.yaml")["base_model"] == \
        "MediaTek-Research/Breeze-ASR-25"


def test_scripts_read_the_export(export):
    rows = common.read_split(export / "asr", "train")
    assert rows and Path(rows[0]["path"]).exists() and len(common.load_wav(rows[0]["path"])) > 16000
    chat = common.read_chat(export / "mt", "zh-en", "train")
    assert chat and chat[0]["messages"][-1]["role"] == "assistant"
    assert common.corpus_cer(["禪修"], ["禪修"]) == 0.0 and common.corpus_cer(["a"], ["禪修"]) == 1.0
    bt = common.ByteTokenizer()
    assert bt.decode(bt.encode("止觀 zen")) == "止觀 zen"


@pytest.mark.parametrize("script,sub", [("train_asr_lora.py", "asr"), ("train_mt_lora.py", "mt")])
def test_cpu_dry_run(export, tmp_path, script, sub):
    py = _py()
    if not py:
        pytest.skip("torch/transformers/peft not installed (set ZEN_TRAIN_PYTHON)")
    out = tmp_path / sub
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "CUDA_VISIBLE_DEVICES": "",
           "TOKENIZERS_PARALLELISM": "false"}
    p = subprocess.run([py, str(SCRIPTS / script), "--data", str(export / sub), "--out", str(out), "--dry-run"],
                       capture_output=True, text=True, timeout=900, env=env)
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-4000:]
    rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert rep["dry_run"] is True and rep["steps"] >= 1
    assert (out / "adapter" / "adapter_config.json").exists()
    if sub == "asr":
        assert rep["cer_before"] is not None and rep["cer_after"] is not None
        assert rep["trainable_params"] < rep["total_params"]


def test_conversion_scripts_plan_only(tmp_path):
    for script in ("convert_asr_to_ggml.py", "convert_mt_to_gguf.py"):
        p = subprocess.run([sys.executable, str(SCRIPTS / script), "--merged", str(tmp_path / "m"), "--out",
                            str(tmp_path / "o"), "--plan"], capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, p.stderr
        assert "convert" in p.stdout.lower()
