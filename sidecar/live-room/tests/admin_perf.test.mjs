// 即時效能頁純函式測試（uiux-a）。node tests/admin_perf.test.mjs
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {
  adaptMetrics, announcement, buildView, connText, fmtDuration, gateAnnouncement, initialState, isEmpty, niceScale,
  problemText, reduce, retryDelay, rtfLevel, stageLevel, stepHysteresis, RTF_TEXT, STAGES,
} from "../app/admin/static/perf_logic.js";

const fixture = JSON.parse(readFileSync(new URL("../app/admin/static/perf_fixture.json", import.meta.url), "utf8"));

// --- RTF 門檻（host.html:470-476）
assert.equal(rtfLevel(0.69), "ok");
assert.equal(rtfLevel(0.7), "warn");
assert.equal(rtfLevel(0.89), "warn");
assert.equal(rtfLevel(0.9), "bad");
assert.equal(rtfLevel(3), "bad");
assert.equal(rtfLevel(null), "none");
assert.equal(rtfLevel(NaN), "none");
assert.equal(rtfLevel("x"), "none");
assert.equal(RTF_TEXT.ok, "跟得上");
assert.equal(RTF_TEXT.warn, "接近上限");
assert.equal(RTF_TEXT.bad, "跟不上，字幕會延遲");

// --- 各段門檻（round4 §3.2）
assert.deepEqual(STAGES.map((s) => s.id), ["A2", "A3", "A4", "A5", "A6"]);
assert.equal(stageLevel("A2", 299), "ok");
assert.equal(stageLevel("A2", 300), "warn");
assert.equal(stageLevel("A2", 600), "bad");
assert.equal(stageLevel("A3", 5999), "warn");
assert.equal(stageLevel("A3", 6000), "bad");
assert.equal(stageLevel("A4", 149), "ok");
assert.equal(stageLevel("A5", 4199), "ok");
assert.equal(stageLevel("A5", 4200), "warn");
assert.equal(stageLevel("A5", 5400), "bad");
assert.equal(stageLevel("A6", 1600, "en"), "warn");
assert.equal(stageLevel("A6", 1600, "ja"), "ok");
assert.equal(stageLevel("A6", 3500, "ja"), "warn");
assert.equal(stageLevel("A6", null), "none");
assert.equal(stageLevel("ZZ", 1), "none");

// --- 格式
assert.equal(fmtDuration(850), "850 ms");
assert.equal(fmtDuration(1310), "1.31 s");
assert.equal(fmtDuration(12345), "12.3 s");
assert.equal(fmtDuration(null), "—");
assert.equal(fmtDuration(-5), "—");
assert.deepEqual(niceScale(4600), {max: 5000, ticks: [0, 1250, 2500, 3750, 5000]});
assert.equal(niceScale(0).max, 500);
assert.equal(niceScale(5001).max, 10000);
assert.equal(retryDelay(0), 2000);
assert.equal(retryDelay(1), 4000);
assert.equal(retryDelay(9), 10000);

// --- 真實形狀：latency = app/latency.py StageLatency.snapshot()（fixture 是同形狀的假數字）
const lat = adaptMetrics(fixture, 1000);
assert.equal(lat.kind, "latency");
assert.equal(lat.stages.length, 5);
assert.deepEqual(lat.stages[0], {id: "A2", p50: 120, p95: 260, n: 48});
assert.deepEqual(lat.rtf, {p50: 0.6, p95: 0.77, n: 48});
const fv = buildView(lat);
assert.equal(fv.empty, false);
assert.equal(fv.rtf.level, "warn");
assert.equal(fv.rtf.text, "接近上限");
assert.equal(fv.rows.find((r) => r.id === "A6").level, "bad");
assert.equal(fv.rows.find((r) => r.id === "A2").level, "ok");
assert.equal(fv.rows.find((r) => r.id === "A3").level, "warn");
assert.equal(fv.scale.max, 5000);
assert.equal(fv.backlog, "2.4 s");
for (const r of fv.rows) {
  assert.ok(r.p95Pct >= r.p50Pct, `${r.id} p95 bar must reach at least p50`);
  assert.ok(r.p95Pct <= 100);
  assert.match(r.aria, new RegExp(`^${r.id} `));
}
// tgt_lang=ja 時 A6 門檻放寬（目前 /api/metrics 沒有 tgt_lang，將來有就會套用）
assert.equal(buildView(adaptMetrics({...fixture, tgt_lang: "ja"})).rows.find((r) => r.id === "A6").level, "warn");
// latency.snapshot() 沒樣本：n=0、p50_ms/p95_ms=null
const zero = {latency: Object.fromEntries(["A2", "A3", "A4", "A5", "A6"].map((k) => [k, {name: k, n: 0, p50_ms: null, p95_ms: null}])),
              asr_samples: 0, asr_rtf_p50: null, asr_rtf_p95: null};
assert.equal(isEmpty(adaptMetrics(zero)), true);
assert.equal(buildView(adaptMetrics(zero)).rows[0].levelText, "尚無資料");
// 部分段有資料（例如 A6 還沒翻譯）
const partial = adaptMetrics({...fixture, latency: {...fixture.latency, A6: {name: "mt", n: 0, p50_ms: null, p95_ms: null}}});
assert.equal(partial.stages[4].p95, null);
assert.equal(buildView(partial).rows[4].level, "none");
// 有 rtf.session 時以「這一場」為準；count 0 → 不顯示上一場的數字
const sess = adaptMetrics({...fixture, rtf: {session: {count: 5, rtf: {p50: 0.3, p95: 0.5}}}});
assert.deepEqual(sess.rtf, {p50: 0.3, p95: 0.5, n: 5});
const newSession = adaptMetrics({...zero, asr_rtf_p95: 0.8, asr_samples: 9, rtf: {session: {count: 0, rtf: {p50: null, p95: null}}}});
assert.equal(newSession.rtf.p95, null);
assert.equal(isEmpty(newSession), true);
// 舊版 live（沒有 latency）：各段尚無資料、RTF 照常；單一 rtf 數字也收
const legacy = adaptMetrics({asr_rtf_p50: 0.42, asr_rtf_p95: 0.95, asr_samples: 12});
assert.equal(legacy.kind, "legacy");
assert.ok(legacy.stages.every((x) => x.p95 === null));
assert.equal(buildView(legacy).rtf.level, "bad");
assert.equal(adaptMetrics({rtf: 0.4}).rtf.p95, 0.4);
// p95 < p50 防呆、負數／字串
assert.equal(adaptMetrics({latency: {A2: {n: 2, p50_ms: 500, p95_ms: 100}}}).stages[0].p95, 500);
assert.equal(adaptMetrics({latency: {A2: {n: 2, p50_ms: -1, p95_ms: "x"}}}).stages[0].p95, null);
// 垃圾輸入
assert.equal(adaptMetrics(null).kind, "unknown");
assert.equal(adaptMetrics([1, 2]).kind, "unknown");
assert.equal(isEmpty(adaptMetrics({})), true);
assert.equal(buildView(adaptMetrics({latency: {}})).empty, true);
assert.equal(buildView(adaptMetrics({latency: {}})).rtf.text, "開始聽之後才有數字");

// --- 錯誤訊息（problem+json detail）
assert.equal(problemText(503, {detail: "直播服務（8780）目前連不上"}), "直播服務（8780）目前連不上");
assert.match(problemText(401, {detail: "需要登入或後台權杖"}), /重新登入/);
assert.match(problemText(0, null), /連不上後台/);
assert.match(problemText(500, null), /HTTP 500/);
assert.match(problemText(503, null), /暫時無法/);
assert.match(problemText(403, null), /權限不足/);

// --- 狀態機：loading → ready → stale → ready；空狀態；錯誤
let s = initialState();
assert.equal(s.phase, "loading");
assert.match(connText(s), /載入中/);
const e1 = reduce(s, {type: "fail", status: 503, body: {detail: "直播服務（8780）目前連不上"}, now: 1});
assert.equal(e1.phase, "error");
assert.equal(e1.stale, false);
assert.equal(e1.error, "直播服務（8780）目前連不上");
s = reduce(s, {type: "ok", body: fixture, now: Date.UTC(2026, 9, 10, 15, 0, 0)});
assert.equal(s.phase, "ready");
assert.equal(s.rtf.stable, "warn");
assert.match(connText(s), /每 2 秒更新｜最後更新 \d\d:\d\d:\d\d/);
assert.match(connText(s, "1"), /^示範資料（假資料）/);
const st = reduce(s, {type: "fail", status: 0, body: null, now: 99});
assert.equal(st.stale, true);
assert.equal(st.phase, "ready");                 // 不清空舊數字
assert.equal(st.view, s.view);
assert.match(connText(st), /（數字暫停更新）最後更新/);
const au = reduce(s, {type: "fail", status: 401, body: {detail: "x"}, now: 99});
assert.equal(au.auth, true);
assert.equal(au.stale, true);
const back = reduce(st, {type: "ok", body: fixture, now: 200});
assert.equal(back.stale, false);
assert.equal(back.failures, 0);
const empty = reduce(initialState(), {type: "ok", body: zero, now: 1});
assert.equal(empty.phase, "empty");
assert.equal(empty.rtf.stable, "none");

// --- 遲滯：連續 3 筆才換等級
let h = {stable: "none", candidate: null, count: 0};
h = stepHysteresis(h, "ok"); assert.equal(h.stable, "ok");
h = stepHysteresis(h, "warn"); assert.equal(h.stable, "ok");
h = stepHysteresis(h, "warn"); assert.equal(h.stable, "ok");
h = stepHysteresis(h, "ok"); assert.equal(h.stable, "ok"); assert.equal(h.count, 0);
h = stepHysteresis(h, "bad"); h = stepHysteresis(h, "bad"); assert.equal(h.stable, "ok");
h = stepHysteresis(h, "bad"); assert.equal(h.stable, "bad");

// --- 播報：只在等級／暫停狀態改變時
const ok = (p95) => ({latency: {A5: {name: "asr", n: 1, p50_ms: 1, p95_ms: 2}}, asr_rtf_p50: 0.1, asr_rtf_p95: p95, asr_samples: 1});
let a = reduce(initialState(), {type: "ok", body: ok(0.5), now: 1});
assert.equal(announcement(initialState(), a), "辨識速度：跟得上");
let said = [];
let prev = a;
for (let i = 0; i < 10; i++) {                   // 同一等級、數字一直變：不唸
  const n = reduce(prev, {type: "ok", body: ok(0.5 + i * 0.01), now: 2 + i});
  const msg = announcement(prev, n);
  if (msg) said.push(msg);
  prev = n;
}
assert.deepEqual(said, []);
said = [];
for (const v of [0.8, 0.8, 0.8, 0.8, 0.8]) {     // 變黃：第 3 筆才唸一次
  const n = reduce(prev, {type: "ok", body: ok(v), now: 50});
  const msg = announcement(prev, n);
  if (msg) said.push(msg);
  prev = n;
}
assert.deepEqual(said, ["辨識速度：接近上限"]);
const down = reduce(prev, {type: "fail", status: 0, body: null, now: 60});
assert.equal(announcement(prev, down), "數字暫停更新：連線中斷，正在重試");
const down2 = reduce(down, {type: "fail", status: 0, body: null, now: 62});
assert.equal(announcement(down, down2), null);  // 持續斷線不重複唸
const up = reduce(down2, {type: "ok", body: ok(0.8), now: 64});
assert.equal(announcement(down2, up), "即時數字已恢復更新");
const expired = reduce(up, {type: "fail", status: 401, body: null, now: 66});
assert.equal(announcement(up, expired), "數字暫停更新：登入已過期");
const err = reduce(initialState(), {type: "fail", status: 503, body: {detail: "直播服務（8780）目前連不上"}, now: 1});
assert.equal(announcement(initialState(), err), "無法取得即時效能：直播服務（8780）目前連不上");

// --- 30 s 內同一句不重複
let g = gateAnnouncement(null, "辨識速度：接近上限", 0);
assert.equal(g.say, "辨識速度：接近上限");
g = gateAnnouncement(g.memo, "辨識速度：接近上限", 10000);
assert.equal(g.say, null);
g = gateAnnouncement(g.memo, "辨識速度：接近上限", 31000);
assert.equal(g.say, "辨識速度：接近上限");
assert.equal(gateAnnouncement(g.memo, null, 40000).say, null);

console.log("admin_perf.test.mjs: ok");
