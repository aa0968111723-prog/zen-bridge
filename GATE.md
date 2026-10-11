# 品質閘門 — 第 2 批

- 時間：2026-10-11 01:25–01:40（Asia/Taipei），執行：cto，只讀
- 閘門對象：筆電 `grok/integrate` @ **b376050**（b376050c52ca286fe21ddc6379b5514bf82ad457）。乾淨的 detached worktree 建在 `C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\cto-gate2`（b376050），所有測試都在這裡跑，一次一個檔，`-p no:cacheprovider`、`PYTHONDONTWRITEBYTECODE=1`。跑完 `git status` 是乾淨的。可用記憶體 5065–5216 MB（筆電量測）。
- log：筆電 `...\zen-bridge-impl\cto-patch\gate2-logs\`；8645 探針在 `...\cto-patch\gate2\probe_8645.py` 和 `probe_8645.out.txt`（box 上也有一份 `/workspace/zen-impl/cto/gate2/probe_8645.py`）
- 整合者說明檔的實際位置：`C:\Users\MacMiRyzen5\Documents\Codex\2026-10-05\c-users-macmiryzen5-downloads-breeze-live\work\coordination\GROK_INTEGRATE_READY.md`（01:22 寫入）。不在 zen-bridge-impl，也不在 zen-bridge-grok 底下。

## 總判定：**b376050 PASS**
- 沒有新的 P1。
- b376050 範圍內沒有新的 P0。
- **閘門進行中整合分支又往前推到 c4ddf89（01:36），裡面有一項 P0 候選**，見 §6。

## 1. head 與工作區 — PASS
- 開始檢查時 `grok/integrate` = b376050，`git --no-optional-locks status` 乾淨。
- 01:40 再查，已經前進到 **c4ddf89**（多了 2351615、c4ddf89 兩個 commit），工作區仍然乾淨。

## 2. 交件內容是否在 head — PASS
| 項目 | 結果 |
|---|---|
| impl/cto 4850fdc | 是 b376050 的祖先（396a903 merge）。`ACCEPTANCE.zh-TW.md` 和 `GATE.md` 和 4850fdc 完全相同；NOTES 移到 `docs/impl-notes/cto.md`（含 1b89eef、141489e） |
| backup（box patch 83f98a88…） | 把 patch 套在 9743ce8 的 archive 副本上，再和 head 逐檔比對：`test_restore_drill.py`、`RESTORE_DRILL.zh-TW.md` 完全相同；`restore_drill.py` 只差 1 行（86f8680 依 CISO P2-1 把表名加上引號） |
| cicd（0b65df9d…） | 只多了 `with: persist-credentials: false` 兩行 |
| ciso（box 新版 c61e7fe0…，第 2 輪） | `THREAT_MODEL.zh-TW.md` 和 head 完全相同 |
| arch（5382a4e5…，3 個 commit） | `ARCHITECTURE_LIVE.zh-TW.md` 和 head 完全相同 |

## 3. 示意圖 8645／本機限制修法 — PASS
- diff（9743ce8..b376050，`app/visual.py`、`app/visual_routes.py`、`app/server.py`）：
  - `is_loopback_url` 依序檢查：`effective_port` 在 `RESERVED_PORTS` 裡就拒絕；帶帳密就拒絕；再跑一次 `translate_config.validate_base_url`（不看 ALLOW_REMOTE，也拒絕 query 和 fragment）；最後只接受 `localhost`／`127.0.0.1`／`::1`，或 `ipaddress(...).is_loopback`。
  - `_post` 每次呼叫前都重新檢查，然後用 `app.net.safe_opener()`（`ProxyHandler({})` 加 `NoRedirect`）。
  - `GET /api/visual/{room}` 加上 `_visual_http_guard`（檢查 Host，再用和 WS 相同的 `_listener_authorized`，要 `?k=` 或主持權杖）。
- **探針（筆電實跑，不連 8645）：BAD_TOTAL 0。** 34 種都被拒絕（`is_loopback_url` False，而且 `build_llm_from_env` 回傳 None）：
  - 8645 的各種寫法：127.0.0.1、localhost、LOCALHOST、[::1]、0.0.0.0、127.1、[::ffff:127.0.0.1]、[0:0:0:0:0:0:0:1]、`:08645`、`:+8645`、`: 8645`、https、`localhost.`、127.0.0.2、全形句點、zone id、空主機。
  - 非本機或變形：0.0.0.0:11434、127.1、2130706433、0x7f.0.0.1、017700000001、`localhost.`、127.evil.example、127.0.0.1.nip.io。
  - 帳密或 userinfo 混淆：`u:p@`、`evil@…:8645`、`127.0.0.1:8645@evil`、`\@`、`[::1]:8645@127.0.0.1:11434`。
  - 其他：query、fragment、ftp、port 65536。
  - 4 種合法的本機位址仍然允許。
  - **轉址**：本機假伺服器回 307 指向另一個本機伺服器，結果是 HTTPError 307，目標收到 0 次請求（沒有跟隨）。
  - **proxy**：設了 `HTTP_PROXY` 指向本機假 proxy，proxy 收到 0 次請求，直接連目標。
  - **呼叫時檢查**：建好用戶端後把 base_url 改成 8645，在連線前就丟 ValueError（opener 用 stub 換掉，確認沒有呼叫）。
- 測試（b376050 worktree）：`test_visual_8645.py` 15 passed；`test_visual_routes.py` 31 passed；`test_listen_key.py` 26 passed。
- 殘留 P2（ciso 第 2 輪提過，**還沒修**）：`POST /api/visual/{room}/trigger` 還是用預設的 `local_only_host_guard`，`server.py` 沒有傳 `host_guard`，所以沒有 CSRF 或主持權杖檢查。

## 4. 計時測試「沒有放寬」— PASS（附 P2 備註）
- 9743ce8..b376050 的 tests diff 裡**沒有新增 skip、xfail 或 rerun**。
- 門檻數字沒有調大：
  - `waiting_v_total < 1`、`oldest_wait ≤ vlimit(15)`、`p50 ≤ 10`、`p95 ≤ 15`、`max ≤ 20`、`drift ≤ 3`、`elapsed_v ≤ vlimit(2.5)`、SRT `|last_end − N·6| ≤ 2` 都和原來一樣。
  - RSS 每個視窗 < 10 MB、總量 < 40 MB 也保留。
- 改的是量測方式（`tests/sim.py`）：
  1. gc.collect 佔用事件迴圈的時間，原樣還給在途的上傳，不計入量測；
  2. Linux 上扣掉 run-queue 搶不到 CPU 的時間（Windows 上是 0）；
  3. `test_push_returns_before_slow_translation` 先在別的房間暖身一次，計時改用 perf_counter。
- **P2 備註**：`test_100min_rss_bounded` 的「不加速成長」斷言，從 RSS 視窗（`windows[3] ≤ max(windows[:3]) + 2 MB`）**換成** `sys.getallocatedblocks()` 判準（≤ 穩態 × 1.25 + 2000，另加整體斜率檢查）。這是換了判準，不是把同一個數字調大，RSS 上限也都還在；但原本那條 RSS 加速斷言已經不存在了。另外，「把 gc 時間還給在途上傳」會讓每 50 段裡最多 gc 耗時那麼長的 backlog 看不到。兩點都建議柏能知悉。
- 單獨跑（筆電，b376050）：`test_sim_backlog.py` **16 passed**（303 s）；`test_sim_srt.py` **6 passed**（128 s）；`test_sim_translation.py` **13 passed**（8.7 s）。

## 5. cicd workflow — PASS
- `actions/checkout` 設了 `persist-credentials: false`；`permissions: contents: read`；觸發是 `pull_request`。
- 4 個 `uses:`（checkout、setup-python、setup-node、upload-artifact）都用 40 位 SHA 釘選，並附版本註解。
- 6ec5012..b376050 的 `.github/` 只新增了這一個檔，沒有改 desktop.yml。

## 6. 禁碰 Codex 檔
- **9743ce8..b376050：0 筆**（比對 BRIEF 清單：desktop/、updater、installer、setup.iss、build_setup.py、update_runtime.py、desktop.yml、字典／glossary 檔、glossary.py、host_glossary.js）。6ec5012..b376050 也是 0 筆。
- round4 §7 的 `app/server.py`、`host.html` 有改，這些是執行手（整合者）自己的檔案，不算違規。
- **P0 候選（b376050 之後，需要柏能裁定）**：整合者在 **2351615**（01:36，作者 "Grok QA"）改了 **`sidecar/live-room/app/glossary.py`**（+22 行，詞彙加上選填的 `ja`、`reading`）。BRIEF 把「字典／glossary 檔」列為 Codex 禁碰。如果柏能認為 round4 #7 授權整合者改 glossary.py，就降為「已知會」；如果沒有授權，就是 P0，請整合者回退或改成新模組。這個檔上一次修改是 6335f40（Bruce，10-09）。
- 新程式的網路靜態掃描（9743ce8..b376050）：只有 `socket.gethostname()`（QR 碼的電腦名稱，不會連線），沒有新的對外呼叫。

## 7. 上批 P2 追蹤
| 項目 | 狀態 |
|---|---|
| ciso REVIEW.md 更新 | **已更新**：第 2 輪 00:04–00:40，涵蓋筆電 9 個分支，aitest 的 2 個 P1 也已經由整合者修好（見 §3） |
| dbtest NOTES 補自己的 sha | **未完成**：`docs/impl-notes/dbtest.md` 只寫 bea2e5c，沒有 33c9fce |
| 五組 NOTES 收進 repo（perf、dba、uiux-a、uiux-b、testlead） | **未完成**：`docs/impl-notes/` 只有 aitest、backend、cto、dbtest、fullstack |
| cicd persist-credentials | 已修（86f8680） |
| backup 表名引號（CISO P2-1） | 已修（86f8680） |

## 8. ACCEPTANCE P0（28 項）現況（b376050）
| 編號 | 狀態 | 依據 |
|---|---|---|
| T-01 全套 0 failed | **PASS（依整合者回報）** | READY：1333 passed／0 failed／6 skipped（skip 比基準少）。cto 依規定沒有重跑全套 |
| T-02 每件有測試且全套 0 failed | **PASS** | 第 1、2 批閘門；純文件件 N/A |
| T-03 Windows py3.11＋3.12 | **待柏能實機／CI** | 筆電只有 3.12；workflow 沒 push，所以 CI 沒跑過 |
| T-07 桌面煙霧測試 | **待柏能實機**（`--real`） | `test_smoke_desktop.py` 15 passed、1 skipped（real） |
| R-01 CER | 待柏能實機 | — |
| R-02 草稿決定性 | 待柏能實機 | `rtf_check.py det` |
| R-03 100 分鐘場次 | 待柏能實機 | sim 測試在筆電通過（§4），只能當參考 |
| R-04 私密暫停 | **待柏能實機**（自動測試部分 PASS） | `test_private_pause.py` 4 passed |
| P-01 RTF p95 < 0.9 | 待柏能實機 | — |
| P-02 中文延遲 | 待柏能實機 | — |
| P-03 譯文延遲 | 待柏能實機 | — |
| P-06 MT 單句 | 待柏能實機 | — |
| P-07 排隊 A3 | 待柏能實機 | — |
| S-01 後台只綁本機、拒絕 8645 | **PASS** | `test_admin_api.py -k "bind or 8645"` 5 passed（netstat 部分要實機） |
| S-03 沒有新增網路呼叫 | **PASS** | 第 1、2 批靜態掃描 |
| S-04 沒有付費模型 | **PASS** | 同上 |
| S-05 log 不含秘密 | **PASS** | `test_security_ciso.py` 30 passed；`test_logfile.py` 2 passed |
| S-06 git 沒有秘密 | **PASS** | `git grep` 命中 1 筆，是 ACCEPTANCE 第 91 行 S-06 自己寫的正則字串（誤報）；`.env` 已被 ignore |
| S-09 語料去個資 | **PASS** | `test_corpus_export.py` 37 passed |
| S-10 staging 核准 | **PASS** | `test_admin_staging.py` 26 passed、1 skipped |
| B-01 自動備份 | **PASS** | `test_backup_restore.py` 12 passed；`test_desktop_maint.py` 8 passed |
| B-02 還原演練工具 | **PASS** | `test_restore_drill.py` 11 passed |
| D-01 NOTES 齊全 | **FAIL（輕微，補文件即可）** | dbtest 缺自己的 sha；5 組 NOTES 沒進 repo（§7） |
| D-02 安裝文件第三人照做 | 待柏能實機 | — |
| M-03 metrics A2–A7、B1 | **FAIL** | `app/latency.py` STAGES 只有 A2–A6；A7（推送＋繪製）和 B1（草稿第一字）還沒有量測點 |
| F-backend | **PASS** | staging 26 passed；`test_migrations_v3.py` 11 passed（0010–0012） |
| F-backup | **待柏能實機**（B-02 PASS，B-03 RTO 要實機計時） | — |
| F-testlead | **待柏能實機**（T-07 real） | — |

小計：**PASS 12、FAIL 2（D-01、M-03）、待柏能實機 14**（T-03、T-07、R-01～R-04、P-01、P-02、P-03、P-06、P-07、D-02、F-backup、F-testlead）。

## 待辦
1. 柏能裁定 2351615 改 `app/glossary.py` 的事（§6）。
2. exec：M-03 補上 A7 和 B1 的量測點和測試。
3. 整合者：把 perf、dba、uiux-a、uiux-b、testlead 的 NOTES 收進 `docs/impl-notes/`；dbtest NOTES 補上 33c9fce。
4. aitest／整合者（P2）：`/api/visual/{room}/trigger` 傳入 `host_guard=require_host`。
5. 柏能實機：先跑 T-07 `--real`、P-01 RTF，以及 R-03 100 分鐘場次。

---

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
