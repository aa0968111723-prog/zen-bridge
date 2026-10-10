# 本機翻譯 profile 與量化（round4 #10、Index-Translate）

| profile | `BREEZE_MT_BACKEND` | 模型 | 預設？ | 範本 |
|---|---|---|---|---|
| 關閉 | `off`（預設） | — | ✅ | — |
| Hy-MT2-1.8B | `hymt` | llama-server | 否 | 模型卡原文（round3 C6） |
| Index-Translate-2B | `index` | llama-server（Qwen3.5 混合架構，**不能用 Ollama**） | 否，ja 候選 | 模型卡原文；`temperature=0`；`chat_template_kwargs={"enable_thinking": false}`；回覆會去掉 think 區塊 |

## 量化（D3）

- **ja 場次預設 Q8_0**：Hy-MT2 官方 Table 5 顯示 Q4_K_M 的 IFMTBench（術語、格式等指令遵循）從 69.36 掉到 63.47；我們靠術語表，所以 ja 用 Q8_0。
- 代價（box Xeon 實測，不是 5600H）：Q8_0 生成速度 12.3 tok/s，Q4_K_M 19.2 tok/s。
- **en 先維持 Q4_K_M**，等筆電 T4 的 MT 層數字出來再決定。
- `mt_backend.gguf_for(lang, profile)` 會回傳 llama-server 該載入的檔名；可以用 `BREEZE_MT_GGUF_EN`／`BREEZE_MT_GGUF_JA` 覆寫成完整路徑。
- 程式**不會下載**任何 GGUF；要哪個檔案，先經柏能同意再手動放進去。

## Index-Translate 的範本（照模型卡）

- 沒有約束時：`请将以下{源語言}文本翻译为{目標語言}，直接输出翻译结果，不要进行任何解释。\n\n{原文}`
- 有術語或上文時，用 instTrans 格式：`【源文】`／`【约束要求】`。術語寫成 `1. 【硬性要求】专名/术语对照: A→B、C→D`。
- 上文寫成 `【注意】上文（仅供理解语境，不要翻译）：…`。這一條是**我們自己組的**（卡片只說 soft constraint 可以放語境），**尚未驗證**，要放進 20 句人工評估比較。
- 狀態：**只在 sandbox 用假 llama-server 測過**；筆電上沒有跑過，模型也沒有下載。
