# 品質閘門 — 第 1 批

- 時間：2026-10-11 00:12（Asia/Taipei），執行：cto
- 整合分支：`grok/integrate` @ **9743ce8**（筆電 `C:\Users\MacMiRyzen5\Documents\zen-bridge-grok`；`6ec5012` 是它的祖先）
- 各代理分岔點：筆電 9 個 `impl/*` 分支都從 **bea2e5c** 分出（`grok/integrate` first-parent 上的 commit，"round4 #2: T4 prompt 'current' = live app prompt"），而且都已經 merge 進 9743ce8。
- 環境：筆電 DESKTOP-P8RGA3A，共用 venv `zen-bridge-grok\sidecar\live-room\.venv`（Python 3.12.10）。一次只跑一個測試檔，cwd 為各 worktree 的 `sidecar\live-room`，`-p no:cacheprovider`、`PYTHONDONTWRITEBYTECODE=1`。跑完後各 worktree 的 `git status` 和跑之前一樣（沒有寫入）。執行時可用記憶體 5571–5737 MB（筆電量測，Win32_OperatingSystem）。
- 測試 log：筆電 `C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\cto-patch\gate1-logs\`

## 判定總表

| 代號 | 交件位置 | commit／patch | 基底 | 禁碰 | 新增測試 | 網路／付費（靜態） | NOTES | 自己的測試（筆電實跑） | 判定 |
|---|---|---|---|---|---|---|---|---|---|
| perf | 筆電 impl/perf | 15441e2（1 commit） | bea2e5c ✔ | 無 | test_hw_tune.py、test_hw_routes.py | 無網路；subprocess 只查電源計畫 | 齊（sha、測試、來源；限制寫在「只能在筆電上實際驗證的事」） | test_hw_tune 50 passed（0.68 s）；test_hw_routes 3 passed | **PASS** |
| backend | 筆電 impl/backend | b83586e（5 commits） | bea2e5c ✔ | 無（改的是 `app/admin/server.py`，不是 `app/server.py`） | test_admin_staging.py（另改 4 個舊測試的斷言，NOTES 有說明） | 前端 `fetch` 同源 `credentials:"same-origin"`，非外連 | 齊（列出 8f50dc8、d9acf26 等 sha；NOTES 在同一 commit，所以沒有 head sha） | test_admin_staging 26 passed、1 skipped（node 不在 PATH） | **PASS** |
| dba | 筆電 impl/dba | 312faf7 | bea2e5c ✔ | 無 | test_corpus_export.py | 無（`api_key` 命中的是去個資 regex） | 齊；NOTES 未 commit（untracked） | 37 passed（2.69 s） | **PASS** |
| dbtest | 筆電 impl/dbtest | 33c9fce | bea2e5c ✔ | 無 | test_sqlite_live_load.py、live_load_sim.py | 無（只新增 tests） | 大致齊；P2：NOTES 只寫 base bea2e5c，沒寫自己的 commit sha | 4 passed、2 skipped（ZEN_LOAD_HEAVY 才跑），83.8 s | **PASS** |
| backup | box `/workspace/zen-impl/backup/backup.patch` | b558701（sha256 83f98a88… 和 NOTES 一致） | **4b84597**（舊的 box 沙盒本地 HEAD，不是 grok/integrate）；`git apply --check` 在 9743ce8 上通過 | 無 | test_restore_drill.py | 無 | 齊 | 在 9743ce8 的 `git archive` 副本上套用後跑：11 passed（8.22 s） | **PASS**（等整合者搬上筆電） |
| fullstack | 筆電 impl/fullstack | cabb0e6（2 commits） | bea2e5c ✔ | 無 | test_overlay_routes.py、overlay.test.mjs | 無 | 齊（727f35d） | 8 passed、1 skipped（沒有 node） | **PASS** |
| uiux-a | 筆電 impl/uiux-a | a69d855 | bea2e5c ✔ | 無 | test_admin_perf_page.py、admin_perf.test.mjs | 同源 `fetch`；`grok` 命中的是註解裡的分支名 | 齊；NOTES 未 commit | 8 passed、1 skipped（沒有 node） | **PASS** |
| uiux-b | 筆電 impl/uiux-b | 847920e | bea2e5c ✔ | 無（沒改 room.html） | test_room_a11y_css.py | 無 | 齊；NOTES 未 commit | 62 passed（0.25 s） | **PASS** |
| aitest | 筆電 impl/aitest | 17f965b（3 commits） | bea2e5c ✔ | 無 | test_visual_routes.py | **有新增 HTTP 用戶端** `OpenAICompatLLM`（urllib，`app/visual.py`）：只連 loopback（`is_loopback_url`，非 loopback 就停用），預設關閉，沒有付費模型預設值 → 不是 P0。**P1**：沒有拒絕 `127.0.0.1:8645`（ACCEPTANCE S-02） | 齊（9d562b0、6da10c7） | 30 passed（1.23 s） | **PASS**（P1 待修） |
| testlead | 筆電 impl/testlead | 0faa4cd | bea2e5c ✔ | 無 | test_smoke_desktop.py | urllib／socket 只連 `127.0.0.1`（自己啟動的後端和假 MT），停用 proxy，**拒絕 8645**，子程序環境移除 `*_API_KEY` | 齊；NOTES 未 commit | 15 passed、1 skipped（real 模式要實機音檔），35.8 s | **PASS** |
| ciso | box `ciso.patch`＋REVIEW.md | 4ec83af（sha256 fae58af8… 一致） | 6ec5012（box），`apply --check` 9743ce8 通過 | 無 | N/A（純文件 THREAT_MODEL） | 無 | 齊；P2：REVIEW.md 第 1 輪（23:45）仍把 perf／dbtest／testlead／uiux-a／backend／aitest 列成未交，arch 的 sha 還是舊版（5b425716…，現在是 5382a4e5…），要出第 2 輪 | 純文件，未跑 | **PASS**（REVIEW 待更新） |
| cicd | box `cicd.patch` | 315895c（sha256 0b65df9d… 一致） | 6ec5012（box），`apply --check` 9743ce8 通過 | 無（新增獨立 workflow，沒改 desktop.yml） | N/A（workflow 本身就是測試設定） | Actions 都用 40 位 SHA 釘選；P2：checkout 沒設 `persist-credentials: false`（ciso 也提過） | 齊（限制寫在「沒有驗證到的部分」「注意事項與風險」） | 不適用（CI 檔案） | **PASS** |
| arch | box `arch.patch`（3 commits） | 0face99…（sha256 5382a4e5… 和 NOTES 一致） | 6ec5012（box），`apply --check` 9743ce8 通過 | 無 | N/A（純文件 ARCHITECTURE_LIVE） | 無 | 齊（數字標〔推估〕或來源） | 純文件，未跑 | **PASS** |
| cto | 筆電 impl/cto | 1b89eef＋141489e（＋NOTES commit） | 9743ce8 ✔ | 無 | N/A（純文件） | 無 | 齊 | 純文件，未跑測試 | 自評不判 |
| 整合 grok/integrate | 筆電 | 9743ce8 | 6ec5012 是祖先 ✔ | 沒碰 Codex 檔（沒有 desktop/、updater、installer、glossary、build_setup、update_runtime、desktop.yml）；round4 §7 檔是執行手自己的範圍 | 新增／修改 30+ 個測試檔 | 網路只有上面 aitest 的 loopback 用戶端和 testlead 的 loopback；`t4.py` 啟動本機 llama-server 子程序；唯一外部 URL 是 JSON Schema `$schema` 字串（不會連線）；秘密樣式掃描 0 筆；`.env.example` 新增的都是非秘密設定（只檢查名稱和值長度，沒有讀值） | — | 在 impl/cto（程式和 9743ce8 相同）跑 test_static_cache.py：7 passed | **PASS**（全套由整合者跑） |

**FAIL：無。BLOCKED：無。未交：無**（13 個代號都有交件；backup、cicd、ciso、arch 只有 box patch，還沒搬上筆電）。

## P0
**沒有 P0。**
- 沒有代理碰 Codex 檔：靜態 diff 沒有命中；Codex 資料夾沒有執行 git status（避免碰到）。從 `worktree list` 看，Codex 的 head 是 zen-bridge 9b566b9、local-v2 6ec5012、qa a9ca111。
- 沒有代理碰 zen-bridge-grok：`git --no-optional-locks status` 是乾淨的；各分支是用 merge commit 合入，沒有代理直接寫進整合分支。
- 新增的網路呼叫都只連 loopback。

## 給整合者／各代號的待辦（非阻擋）
1. aitest（P1）：`app/visual.py` 的 `is_loopback_url`／`build_llm_from_env` 要拒絕 8645（ACCEPTANCE S-02），並加測試 `BREEZE_VISUAL_LLM_BASE_URL=http://127.0.0.1:8645/v1` 回傳 None。
2. ciso（P2）：REVIEW.md 出第 2 輪，涵蓋筆電上的 9 個分支，並更新 arch 的 sha。
3. cicd（P2）：checkout 加 `persist-credentials: false`。
4. dbtest（P2）：NOTES 補上自己的 commit sha（33c9fce）。
5. 整合者：把 backup、cicd、ciso、arch 的 box patch 搬上筆電（4 份在 9743ce8 上都能乾淨套用）；並在筆電裝 Node ≥ 22，讓 backend／fullstack／uiux-a 的 node 子測試不再 skip（ACCEPTANCE T-04）。
6. perf、dba、uiux-a、uiux-b、testlead 的 NOTES.md 在 worktree 裡是 untracked，merge 時不會帶到；整合者要像其他代號一樣另外收進 `docs/impl-notes/`。

## 重現指令（筆電 PowerShell）
```powershell
$git='C:\Users\MacMiRyzen5\AppData\Local\hermes\tools\git-2.53.0+3-win32-x64\bin\git.exe'
$py='C:\Users\MacMiRyzen5\Documents\zen-bridge-grok\sidecar\live-room\.venv\Scripts\python.exe'
$env:PYTHONDONTWRITEBYTECODE='1'
& $git -C C:\Users\MacMiRyzen5\Documents\zen-bridge-grok diff --name-status bea2e5c impl/<代號>
cd C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\<代號>\sidecar\live-room
& $py -m pytest tests/test_<它的>.py -q -p no:cacheprovider -rs
# box patch：在 impl/cto 上 & $git apply --check <patch>
```
禁碰規則使用 `/workspace/zen-impl/cto/gate.sh` 的 FORBID regex。網路規則：requests／httpx／urllib.request／urlopen／aiohttp／socket／websockets.connect／fetch(／WebSocket(／EventSource(／subprocess，加上非 loopback 的 URL 和付費模型關鍵字；只看新增的行，排除 tests 和 .md，再人工判讀。
