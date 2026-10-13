# 本機 AI 翻譯代理套件

Zen Bridge 是主要介面。進階工具安裝在 `%LOCALAPPDATA%\ZenBridge\agent-suite`，不修改 Grok Bot、Hermes 的 Python。此套件尚未納入穩定版 0.5.1 的自動更新。

## 安裝與狀態

在已安裝 App 的目錄執行 `scripts\install_agent_suite.ps1`，會依序安裝三個獨立環境，固定直接依賴版本、執行依賴檢查，並留下實際版本清單。安裝失敗可重新執行。

- speech：sherpa-onnx、OpenCC、LangGraph。
- evaluation：CPU PyTorch、COMET。
- diarization：CPU PyTorch、pyannote Community-1 執行套件。

安裝套件不代表模型已下載。本人先接受 Hugging Face 模型頁條款，再用 `agent-suite\diarization\Scripts\python.exe` 執行 `scripts/login_agent_models.py`，在官方頁面完成裝置授權。此登入只要求讀取已獲准的公開模型；不要把密碼或 token 放進設定檔、聊天或 GitHub。

接受條款與登入後，用對應環境的 Python 執行 `scripts/download_agent_models.py community1` 或 `cometkiwi`。下載固定到當次查得的模型 commit，完成後才寫就緒標記。下載器不會代替你接受條款。

## 即時流程

`ZEN_AGENT_SUITE_GRAPH=1` 啟用 retrieve → translate → check，保留本機、Hermes 雲端、混合模式。只從課前建立的主題快取取最多三筆參考，不在即時翻譯啟動 embedding。数字、目標語言與時間關係的快速檢查產生疑點標記；最多重試一次。檢查不等於語義準確度保證。

`ZEN_AGENT_SUITE_SPEECH=1` 讓 App 載入已安裝的 speech 可選套件，要求 Python 主次版本一致；原 App 依賴優先。另依現有草稿設定選擇 `BREEZE_DRAFT_ASR=xasr` 和 `BREEZE_DRAFT_MODEL_DIR`。X-ASR int8 保持一個執行緒。草稿不是正式逐字稿。

## 課前與課後工作

先用 Ollama 安裝 `qwen3-embedding:0.6b`。課前執行 `scripts/build_agent_context.py --topic "課程主題"`，將現有中英日參考語句依主題排序。新 App 要回報桌面推論和草稿活動；舊版回報不足時拒絕建立索引。操作台原有資料庫 embedding 暫停保護仍保留。

用 App Python 執行 `scripts/agent_suite_jobs.py status` 檢查狀態。`enhance` 可從本機後台排成課後工作，音檔必須已放在資料庫旁的 `enhance-in`，模型不在就不呼叫。`diarize` 同樣只排課後工作，音檔放在 `diarize-in`，Community-1 未標成已驗證就不呼叫。`evaluate` 只評這一場已存的中英對，最多 64 段，COMET 未標成已驗證就不呼叫，分數寫回譯文並另存 JSON。三者都不進 6 秒即時字幕。嵌入仍不註冊。

## 回復與驗證

0.5.2 候選版附帶 X-ASR 草稿與 SenseVoice int8 快速定稿模型，逐檔 SHA256、來源版本和模型授權隨包保留。SenseVoice 本機檔已對照上游固定 commit 的 LFS／Git 資訊，不另下載 Grok 已取得的權重。完整安裝包含模型子資料夾；舊版依賴不相容時須用完整 Setup。

App 選單「本機快速定稿＋Breeze 校正」預設關閉，下次啟動生效，適用「本機字幕測試頁」。快速字幕及定稿的主 UI relay 仍待整合，不能把本機測試頁的功能當成已接到遠端 Zen 主工作台。校正不覆蓋人工修正或較新的重譯；關閉服務會等待正在讀取音檔的辨識完成再清理暫存。最多四個 CPU 執行緒，草稿 int8 維持單緒。

將兩個 `ZEN_AGENT_SUITE_*` 開關設為 0、重新開 App，即回到既有翻譯及辨識路徑。沒有真實課堂音檔與人工對照文字前，不宣稱完成課堂翻譯品質驗收。模型就緒與套件就緒分別顯示在操作台。
