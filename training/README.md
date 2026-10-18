# 訓練（之後在 GPU 上做；這裡只準備好設定與腳本）

資料來自 `python -m app.training build`（或後台「匯出資料集」），說明見 [docs/finetune-data.md](../docs/finetune-data.md)。**資料只能留在本機**（同意書 v1 寫明不上雲端），所以請用社團自己的 NVIDIA 桌機訓練；要用雲端 GPU 必須先改同意書並重新取得同意。

```
training/
  requirements-train.txt          另開 venv 安裝（不要裝進 App 的 runtime）
  configs/asr_breeze_lora.yaml    Breeze-ASR-25 LoRA
  configs/mt_qwen_lora.yaml       翻譯 LoRA（Qwen3-4B，可改 Qwen3-1.7B）
  scripts/train_asr_lora.py       ASR 微調＋訓練前後 test CER
  scripts/eval_asr_cer.py         CER 評估（HF 模型或 whisper.cpp ggml）
  scripts/train_mt_lora.py        MT 微調（unsloth 或 peft）
  scripts/convert_asr_to_ggml.py  合併模型 → whisper.cpp ggml（App 用）
  scripts/convert_mt_to_gguf.py   合併模型 → GGUF → Ollama／llama-server（App 用）
```

## GPU 需求（估計值，實際依批次大小與音訊長度）

| 工作 | 模型 | 方式 | VRAM | 適合的卡 |
| --- | --- | --- | --- | --- |
| ASR | Breeze-ASR-25（Whisper large-v2，約 15.5 億參數） | LoRA，fp16，batch 4，gradient checkpointing | 約 16–20 GB | RTX 4090／3090（24 GB）、A10／L4（24 GB） |
| ASR | 同上 | LoRA，8-bit 底模（`load_in_8bit: true`），batch 1–2 | 約 10–12 GB | RTX 3060 12 GB、4070（勉強） |
| ASR | 同上 | 全參數微調 | 40 GB 以上 | A100（不建議，LoRA 就夠） |
| MT | Qwen3-4B | QLoRA 4-bit（unsloth） | 約 6–8 GB | RTX 3060 12 GB 以上 |
| MT | Qwen3-4B | LoRA bf16 | 約 16–20 GB | 24 GB 卡 |
| MT | Qwen3-1.7B／Hy-MT 1.8B 級 | QLoRA 4-bit | 約 4–5 GB | 8 GB 卡 |

* 筆電 DESKTOP-P8RGA3A（Ryzen 5 5600H、15 GB RAM、Radeon 內顯）**不能訓練這些模型**：內顯沒有 CUDA，ROCm 不支援這顆 iGPU，記憶體又和系統共用。筆電只負責錄音、審核、匯出，和跑 `--dry-run`。
* 時間參考：RTX 4090 上 10 小時語音、3 epoch 的 Breeze LoRA 約 1–2 小時；每位固定講者有 1 小時以上已校對語音才開始看得到口音改善，5–10 小時較穩。
* 硬碟：Breeze 底模約 3 GB，合併後再一份 3 GB，ggml f16 約 3 GB、q5_0 約 1 GB。

## 步驟

```bash
python -m venv .venv-train && .venv-train/Scripts/activate      # Linux: source .venv-train/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cu124   # 依 CUDA 版本
pip install -r training/requirements-train.txt

# 0) 匯出（在筆電上做，再把 ds_… 資料夾用隨身碟搬到訓練機）
python -m app.training build --out D:\zen-ds1

# 1) ASR
python training/scripts/train_asr_lora.py --data D:/zen-ds1/asr --out runs/asr1
python training/scripts/eval_asr_cer.py --data D:/zen-ds1/asr --model MediaTek-Research/Breeze-ASR-25   # 底模 CER
python training/scripts/eval_asr_cer.py --data D:/zen-ds1/asr --model runs/asr1/merged                  # 微調後 CER
python training/scripts/convert_asr_to_ggml.py --merged runs/asr1/merged --out runs/asr1/ggml \
    --whisper-cpp ~/src/whisper.cpp --openai-whisper ~/src/whisper --quant q5_0
# 在筆電：.env 設 BREEZE_MODEL=<ggml-model-q5_0.bin>，再用 eval_asr_cer.py --whisper-cli 量一次

# 2) MT
python training/scripts/train_mt_lora.py --data D:/zen-ds1/mt --out runs/mt1
python training/scripts/convert_mt_to_gguf.py --merged runs/mt1/merged --out runs/mt1/gguf --llama-cpp ~/src/llama.cpp --create
# 在筆電：BREEZE_TRANSLATE_MODEL=zen-mt-qwen3-4b-lora（Ollama），或 BREEZE_MT_GGUF_EN／_JA=<model-Q4_K_M.gguf>（llama-server）
```

`report.json` 會記錄訓練前後的 CER（ASR）或 exact／chrF3（MT）。CER 沒有下降、或某位講者變差，就不要換上 App。

## Dry-run（CI／沒有 GPU 時）

```bash
python training/scripts/train_asr_lora.py --data <export>/asr --out /tmp/dry-asr --dry-run
python training/scripts/train_mt_lora.py  --data <export>/mt  --out /tmp/dry-mt  --dry-run
python training/scripts/convert_asr_to_ggml.py --merged x --out y --plan
```

`--dry-run` 在本機建一個 1 層的隨機 Whisper／Qwen2 與位元組 tokenizer（不下載任何東西、設 `HF_HUB_OFFLINE=1`），在 CPU 上跑 2 步並算評估，驗證整條程式路徑。測試：`sidecar/live-room/tests/test_training_dryrun.py`（沒有 torch 時只跑設定與資料讀取；設 `ZEN_TRAIN_PYTHON=<有 torch 的 python>` 跑完整 dry-run）。
