# Windows 實機整合狀態

2026-10-10 接收 zen-local-v2；來源 base f2b76f3，patch SHA256
9c9e3447efd168dbcb8365f5576068a3265d66c176d7daaf049a6a6129afc5c8。

## 已完成

- 隔離 checkout 套用 45 個檔案；保留主 checkout 的未提交修改。
- Windows 新功能測試 132 passed、1 skipped（POSIX fake whisper）。
- 四支 PowerShell 腳本通過 Windows PowerShell 5.1 語法檢查；collect_env 已實際執行。
- 後台真實 HTTP smoke：未登入 401、foreign Host 403、query token 400、/docs 404、已授權 health 200、schema 2；綁定 127.0.0.1。測試使用獨立資料庫。
- Silero 真實 ONNX：靜音 speech_ratio=0，公開 JFK 音檔 speech_ratio 約 0.679；不是課堂誤判率驗收。
- ONNX Runtime 1.31.0 與相關依賴已加進 runtime lock。系統 Microsoft C++ DLL 14.32 導致 native import 崩潰；使用 byte-pinned 官方 14.50 x64 app-local runtime 後可載入。同樣舊 DLL 下 1.23.2 也失敗，故未降版。
- app-local runtime 從官方 vc_redist.x64.exe（SHA256 843068991daaa1f73ad9f6239bce4d0f6a07a51f18c37ea2a867e9beca71295c）擷取，12 個 DLL 均驗證 Microsoft Authenticode。安裝時驗證 ZIP 與每個 DLL，不修改系統 DLL。
- VAD 模型 byte hash 驗證；prepare_desktop 會先準備 VAD／C++ runtime，再複製進離線安裝包。
- requirements-local.txt 納入 source package；PowerShell setup 遇到核心 pip／模型下載失敗時停止。
- Mermaid 12.1.0 從官方 npm tarball 擷取，驗證 npm SHA512 integrity；本機 vendored JS 與 MIT LICENSE、SHA256 在 mermaid-manifest.json。只提供精確静態路徑；optional init 固定 strict、startOnLoad=false。不代表示意圖 UI 已完成。

## 未通過的驗收

第一次完整 Windows suite：625 passed、3 skipped、5 failed（634.48 秒）。失敗為 glossary expansion 延遲、100min simulation waiting／latency、512KiB upload latency、SRT end time。沒有放寬門檻。當時另一個 llama-server 占約 6.6 GB，可用 RAM 一度不足 0.5 GB；不可直接推論所有失敗都由此造成，仍需在可比條件重測。

獨立重測 glossary／desktop_update／updater／runtime compatibility：56 passed。

Benchmark preflight：直播 idle，但電池供電；正確 exit 4。Windows stdout cp1252 導致中止提示崩潰已修正為 UTF-8，沒有繞過電池檢查。

尚缺至少 50 段 6 秒中文課堂音訊與人工對照文字，不能提供可靠 CER、p95 或長時間無缺段結論。插電、足夠記憶體、原門檻重測及實際課堂測試後才能發布正式安裝版。

後台安裝、捷徑、自動啟停、Zen 主 UI 整合、Hermes 雲端 403 恢復與真實連續字幕驗收尚未完成；目前已安裝 App v0.4.3 沒有被替換。
