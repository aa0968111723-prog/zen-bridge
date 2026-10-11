# 離線規劃與場次報告

這兩項工具不呼叫模型或雲端 API，也不會自行變更 App 的模型設定。

## 場次報告

在 `sidecar/live-room` 目錄執行：

```powershell
python tools/session_report_md.py --db "C:\Users\你的帳號\AppData\Local\Programs\Breeze Live Room\data\captions.sqlite3" --out "D:\Reports\場次摘要.md"
```

可加 `--session 場次ID` 篩選。報告包含可取得的字幕數、翻譯延遲、閱讀速度超標字幕與術語；缺少欄位時顯示無資料，不估造數值。報告可能包含字幕內容，請自行保管。

資料庫以唯讀方式開啟，整份報告使用同一讀取快照。輸出檔必須尚未存在；工具拒絕覆寫資料庫或其 WAL/SHM/journal。若複製的資料庫帶 WAL 卻缺少 SHM，工具會拒絕分析；請先正常關閉 App，再備份完整資料。標準輸出與檔案均為 UTF-8，可在 Windows 使用。

## 模型測試規劃

```powershell
python tools/mt_model_bench_plan.py --hw hardware.json --out plan.csv --ram-budget-gb 4.8
```

`hardware.json` 使用硬體探測的 `vulkan_devices` 清單。CSV 列出英文/日文、後端、量化、執行緒與 GPU 卸載選項；超出估計 RAM 預算會註記。RAM 是規劃估計，並非電腦實測結果。工具不下載或執行模型。輸出檔已存在時會拒絕覆寫。

原始成果沿用 PR #34 與 #38；日文 ruby 渲染回歸測試沿用 PR #35。日文換行只計算 ruby 的本文寬度，讀音與標記不占主行字寬，整組標記不拆開。
