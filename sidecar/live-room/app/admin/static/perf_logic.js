// 即時效能頁的純函式（不碰 DOM、不連網），給 perf.js 與 tests/admin_perf.test.mjs 共用。
// 門檻來源：
//   - RTF：主持頁 host.html:470-476（p95 <0.7「跟得上」、0.7–0.9「接近上限」、≥0.9「跟不上，字幕會延遲」）。
//   - A2–A6 定義：app/latency.py（docstring 第 3–9 行）；目標：/workspace/zen-research/round4.md §3.2（第 170–174 行）。
//   - 標「暫定」的是本頁自訂的紅色門檻（目標 ×2），要在 5600H 實測後調整。

export const POLL_MS = 2000;
export const HYSTERESIS = 3;          // ui.md §3.1：等級要連續 3 筆相同才切換（暫定）
export const ANNOUNCE_REPEAT_MS = 30000; // ui.md §3.4：30 s 內同一句不重複
export const SLICE_MS = 6000;         // round4 §3.2 A1：host.html periodMs 6000

export const STAGES = Object.freeze([
  {id: "A2", name: "上傳", desc: "片段錄完到伺服器收到", warn: 300, bad: 600,
   note: "目標 p95 < 0.3 s（round4 §3.2）；紅色 0.6 s 為暫定"},
  {id: "A3", name: "排隊", desc: "伺服器收到到開始辨識（含解碼／VAD 與等辨識器）", warn: 1000, bad: 6000,
   note: "目標 p95 < 1 s；> 6 s 代表 RTF > 1、會越積越多（round4 §3.2）"},
  {id: "A4", name: "解碼＋VAD", desc: "webm 解碼＋Silero VAD（＋裁切）", warn: 150, bad: 300,
   note: "目標 p95 < 0.15 s（round4 §3.2）；紅色 0.3 s 為暫定"},
  {id: "A5", name: "辨識（Breeze）", desc: "定稿 ASR 本身", warn: 0.7 * SLICE_MS, bad: 0.9 * SLICE_MS,
   note: "以 6 s 片換算 RTF 0.7／0.9 → 4.2 s／5.4 s（host.html 門檻 × round4 A1 片長）"},
  {id: "A6", name: "翻譯（MT）", desc: "翻譯呼叫本身（不含排隊）", warn: 1500, bad: 3000,
   warnJa: 2000, badJa: 4000,
   note: "目標 p95 < 1.5 s（en）、< 2 s（ja）（round4 §3.2）；紅色 ×2 為暫定"},
]);

export const LEVEL_TEXT = Object.freeze({
  ok: "達標", warn: "超過目標", bad: "嚴重超標", none: "尚無資料",
});

export const RTF_TEXT = Object.freeze({
  ok: "跟得上", warn: "接近上限", bad: "跟不上，字幕會延遲", none: "開始聽之後才有數字",
});

function num(v) {
  if (v === null || v === undefined || v === "" || typeof v === "boolean") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

function int(v) {
  const n = num(v);
  return n === null ? null : Math.round(n);
}

export function stageThresholds(id, lang) {
  const s = STAGES.find((x) => x.id === id);
  if (!s) return null;
  if (id === "A6" && lang === "ja") return {warn: s.warnJa, bad: s.badJa};
  return {warn: s.warn, bad: s.bad};
}

/** p95（毫秒）→ ok / warn / bad / none。 */
export function stageLevel(id, p95ms, lang) {
  const t = stageThresholds(id, lang);
  const v = num(p95ms);
  if (!t || v === null) return "none";
  if (v < t.warn) return "ok";
  if (v < t.bad) return "warn";
  return "bad";
}

/** RTF p95 → 等級；門檻與主持頁一致（host.html:470-476）。 */
export function rtfLevel(p95) {
  const v = num(p95);
  if (v === null) return "none";
  if (v >= 0.9) return "bad";
  if (v >= 0.7) return "warn";
  return "ok";
}

/** 850 → "850 ms"；1310 → "1.31 s"；12345 → "12.3 s"。 */
export function fmtDuration(ms) {
  const v = num(ms);
  if (v === null) return "—";
  if (v < 1000) return `${Math.round(v)} ms`;
  const s = v / 1000;
  return s < 10 ? `${s.toFixed(2)} s` : `${s.toFixed(1)} s`;
}

export function fmtRtf(v) {
  const n = num(v);
  return n === null ? "—" : n.toFixed(2);
}

export function fmtClock(epochMs) {
  const v = num(epochMs);
  if (v === null) return "—";
  const d = new Date(v);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 刻度上限：取 ≥ max 的 1/2/5×10^n，最少 500 ms。回傳 {max, ticks}。 */
export function niceScale(maxMs) {
  const m = Math.max(500, num(maxMs) || 0);
  const pow = Math.pow(10, Math.floor(Math.log10(m)));
  let top = pow;
  for (const f of [1, 2, 5, 10]) {
    if (f * pow >= m) { top = f * pow; break; }
  }
  const step = top / 4;
  const ticks = [0, 1, 2, 3, 4].map((i) => Math.round(i * step));
  return {max: top, ticks};
}

export function pct(ms, max) {
  const v = num(ms);
  if (v === null || !max) return 0;
  return Math.max(0, Math.min(100, (v / max) * 100));
}

function stageFromPair(id, p50, p95, n) {
  let a = num(p50);
  let b = num(p95);
  if (a !== null && b !== null && b < a) b = a;   // 防呆：p95 不可能比 p50 小
  if (a === null && b !== null) a = null;
  return {id, p50: a, p95: b, n: int(n)};
}

/**
 * 把 /admin/api/v1/perf/metrics 的回應（或 8780 /api/metrics 原樣）轉成統一格式。
 * 真實格式（grok/integrate bea2e5c）：
 *   - 各段：`latency` = app/latency.py StageLatency.snapshot()
 *       {"A2":{"name":"upload","n":12,"p50_ms":120,"p95_ms":260}, … "A6":{"name":"mt",…}}
 *     沒有樣本時 n=0、p50_ms/p95_ms=null。由 app/pipeline.py:395 放進 /api/metrics。
 *   - RTF：app/rtf.py RtfMeter.snapshot()；有 rtf.session（最近更新的那一場）優先，
 *     否則用扁平 asr_rtf_p50／asr_rtf_p95／asr_samples（最近 200 段）；舊版單一數字 rtf 也收。
 * 沒有 `latency`（舊版 live）時各段顯示「尚無資料」，RTF 照常。
 */
export function adaptMetrics(raw, nowMs = Date.now()) {
  const out = {kind: "unknown", stages: STAGES.map((s) => ({id: s.id, p50: null, p95: null, n: null})),
               rtf: {p50: null, p95: null, n: null}, backlog_s: null, pending: null, lang: null, updatedAt: nowMs};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  out.lang = raw.tgt_lang === "ja" ? "ja" : (raw.tgt_lang === "en" ? "en" : null);
  out.backlog_s = num(raw.backlog_s);
  out.pending = int(raw.pending);
  const upd = num(raw.updated_at);
  if (upd !== null) out.updatedAt = upd < 1e11 ? upd * 1000 : upd;   // 秒或毫秒都收

  const lat = raw.latency && typeof raw.latency === "object" && !Array.isArray(raw.latency) ? raw.latency : null;
  out.kind = lat ? "latency" : "legacy";
  if (lat) {
    out.stages = STAGES.map((s) => {
      const st = lat[s.id] && typeof lat[s.id] === "object" ? lat[s.id] : {};
      const one = stageFromPair(s.id, st.p50_ms, st.p95_ms, st.n);
      if (one.n === 0) { one.p50 = null; one.p95 = null; }
      return one;
    });
  }

  const sess = raw.rtf && typeof raw.rtf === "object" && raw.rtf.session && typeof raw.rtf.session === "object"
    ? raw.rtf.session : null;
  if (sess) {
    const b = sess.rtf && typeof sess.rtf === "object" ? sess.rtf : {};
    out.rtf = {p50: num(b.p50), p95: num(b.p95), n: int(sess.count)};
  } else {
    let p95 = num(raw.asr_rtf_p95);
    if (p95 === null && typeof raw.rtf === "number") p95 = num(raw.rtf);
    out.rtf = {p50: num(raw.asr_rtf_p50), p95, n: int(raw.asr_samples)};
  }
  if (out.rtf.n === 0) out.rtf = {p50: null, p95: null, n: 0};   // 新的一場沒有樣本：不顯示上一場的數字
  return out;
}

export function isEmpty(adapted) {
  if (!adapted) return true;
  const anyStage = adapted.stages.some((s) => s.p50 !== null || s.p95 !== null);
  return !anyStage && adapted.rtf.p95 === null && adapted.rtf.p50 === null;
}

/** 由統一格式做出畫面模型（純資料），perf.js 只負責把它畫出來。 */
export function buildView(adapted) {
  const lang = adapted && adapted.lang;
  const rows = STAGES.map((s) => {
    const st = (adapted && adapted.stages.find((x) => x.id === s.id)) || {p50: null, p95: null, n: null};
    const t = stageThresholds(s.id, lang);
    const level = stageLevel(s.id, st.p95, lang);
    return {id: s.id, name: s.name, desc: s.desc, note: s.note, p50: st.p50, p95: st.p95, n: st.n,
            warn: t.warn, bad: t.bad, level, levelText: LEVEL_TEXT[level],
            p50Text: fmtDuration(st.p50), p95Text: fmtDuration(st.p95)};
  });
  const maxVal = Math.max(0, ...rows.map((r) => Math.max(r.p95 || 0, r.p50 || 0)));
  const scale = niceScale(maxVal);
  for (const r of rows) {
    r.p50Pct = pct(r.p50, scale.max);
    r.p95Pct = pct(r.p95 === null ? r.p50 : r.p95, scale.max);
    r.warnPct = pct(r.warn, scale.max);
    r.warnInScale = r.warn <= scale.max;
    r.aria = r.p95 === null && r.p50 === null
      ? `${r.id} ${r.name}：尚無資料`
      : `${r.id} ${r.name}：一般 ${r.p50Text}，最慢 ${r.p95Text}，${r.levelText}`;
  }
  const rtf = adapted ? adapted.rtf : {p50: null, p95: null, n: null};
  const rl = rtfLevel(rtf.p95);
  return {
    kind: adapted ? adapted.kind : "unknown",
    empty: isEmpty(adapted),
    rows, scale: {max: scale.max, ticks: scale.ticks.map((t) => ({ms: t, pct: pct(t, scale.max), text: fmtDuration(t)}))},
    rtf: {p50: rtf.p50, p95: rtf.p95, n: rtf.n, level: rl, text: RTF_TEXT[rl],
          p50Text: fmtRtf(rtf.p50), p95Text: fmtRtf(rtf.p95)},
    backlog: adapted && adapted.backlog_s !== null ? `${adapted.backlog_s.toFixed(1)} s` : "—",
    pending: adapted && adapted.pending !== null ? String(adapted.pending) : "—",
    lang,
  };
}

/** 非 2xx → 可讀訊息；優先用 problem+json 的 detail。 */
export function problemText(status, body) {
  const detail = body && typeof body === "object" && typeof body.detail === "string" && body.detail.trim()
    ? body.detail.trim().slice(0, 300) : "";
  if (status === 401) return "登入已過期或尚未登入，請重新登入後台。";
  if (status === 403) return detail || "權限不足，無法查看即時效能。";
  if (status === 429) return detail || "請求太頻繁，稍後會自動重試。";
  if (detail) return detail;
  if (status === 0) return "連不上後台（網路中斷或後台已停止），會自動重試。";
  if (status === 503) return "直播服務暫時無法使用，會自動重試。";
  if (status >= 500) return `伺服器錯誤（HTTP ${status}），會自動重試。`;
  return `讀取失敗（HTTP ${status}）。`;
}

/** 錯誤重試間隔：2、4、8、10、10… 秒。 */
export function retryDelay(failures) {
  const f = Math.max(0, failures | 0);
  return Math.min(10000, POLL_MS * Math.pow(2, f));
}

export function initialState() {
  return {phase: "loading", data: null, view: null, lastOk: null, error: null, status: null,
          failures: 0, stale: false, auth: false,
          rtf: {stable: "none", candidate: null, count: 0}};
}

/** RTF 等級遲滯：連續 need 筆相同才換；第一次有數字（stable=none）直接採用。 */
export function stepHysteresis(h, raw, need = HYSTERESIS) {
  if (raw === h.stable) return {stable: h.stable, candidate: null, count: 0};
  if (h.stable === "none" || raw === "none") return {stable: raw, candidate: null, count: 0};
  const count = h.candidate === raw ? h.count + 1 : 1;
  if (count >= need) return {stable: raw, candidate: null, count: 0};
  return {stable: h.stable, candidate: raw, count};
}

/**
 * 狀態機。action：
 *   {type:"ok", body, now}            成功取得資料
 *   {type:"fail", status, body, now}  非 2xx（status=0 代表網路錯誤／逾時）
 * phase：loading → ready | empty | error；有過資料後失敗 → stale=true（保留舊數字）。
 */
export function reduce(state, action) {
  if (action.type === "ok") {
    const data = adaptMetrics(action.body, action.now);
    const view = buildView(data);
    const rawLevel = view.empty ? "none" : view.rtf.level;
    return {...state, phase: view.empty ? "empty" : "ready", data, view, lastOk: action.now,
            error: null, status: 200, failures: 0, stale: false, auth: false,
            rtf: stepHysteresis(state.rtf, rawLevel)};
  }
  if (action.type === "fail") {
    const auth = action.status === 401;
    const hasData = state.lastOk !== null;
    return {...state, phase: hasData ? state.phase : "error", error: problemText(action.status, action.body),
            status: action.status, failures: state.failures + 1, stale: hasData, auth};
  }
  return state;
}

/** 讀屏播報句：只在 RTF 穩定等級、暫停更新、錯誤狀態改變時回傳字串；否則 null。 */
export function announcement(prev, next) {
  const msgs = [];
  const prevConn = prev.stale ? "stale" : (prev.phase === "error" ? "error" : "ok");
  const nextConn = next.stale ? "stale" : (next.phase === "error" ? "error" : "ok");
  if (prevConn !== nextConn) {
    if (nextConn === "stale") msgs.push(next.auth ? "數字暫停更新：登入已過期" : "數字暫停更新：連線中斷，正在重試");
    else if (nextConn === "error") msgs.push(`無法取得即時效能：${next.error || ""}`.trim());
    else if (prev.phase !== "loading") msgs.push("即時數字已恢復更新");
  }
  if (prev.rtf.stable !== next.rtf.stable && !next.stale) {
    msgs.push(next.rtf.stable === "none" ? "辨識速度：目前沒有數字" : `辨識速度：${RTF_TEXT[next.rtf.stable]}`);
  }
  return msgs.length ? msgs.join("。") : null;
}

/** 30 s 內同一句不重複（ui.md §3.4）。memo = {text, at}；回傳 {say, memo}。 */
export function gateAnnouncement(memo, text, now, repeatMs = ANNOUNCE_REPEAT_MS) {
  if (!text) return {say: null, memo};
  if (memo && memo.text === text && now - memo.at < repeatMs) return {say: null, memo};
  return {say: text, memo: {text, at: now}};
}

/** 頁首連線列文字（看得到，但不在 live region 裡）。 */
export function connText(state, demo) {
  const prefix = demo ? "示範資料（假資料）｜" : "";
  if (state.phase === "loading" && !state.stale) return `${prefix}載入中…`;
  const last = state.lastOk !== null ? `最後更新 ${fmtClock(state.lastOk)}` : "尚未取得資料";
  if (state.stale) return `${prefix}（數字暫停更新）${last}`;
  if (state.phase === "error") return `${prefix}${state.error || "讀取失敗"}`;
  return `${prefix}每 ${POLL_MS / 1000} 秒更新｜${last}`;
}
