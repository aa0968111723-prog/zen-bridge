// 即時效能頁：輪詢 /admin/api/v1/perf/metrics（每 2 秒，分頁隱藏時暫停）。
// 只用 textContent 與 classList／CSS 自訂屬性建 DOM，不解析 HTML 字串、不寫 style 屬性字串（CSP style-src 'self'）。
import {
  POLL_MS, initialState, reduce, announcement, gateAnnouncement, connText, retryDelay, buildView, adaptMetrics,
} from "./perf_logic.js";

const API = "/admin/api/v1/perf/metrics";
const FIXTURE = "/admin/perf/static/perf_fixture.json";
const TIMEOUT_MS = 5000;

const params = new URLSearchParams(location.search);
const demo = params.get("demo");          // "1"｜"stale"｜"empty"｜"error"：示範模式，只讀本頁的假資料檔
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

let state = initialState();
let memo = null;
let timer = null;
let inflight = null;
let stopped = false;

async function fetchJson(url) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  inflight = ctl;
  try {
    const r = await fetch(url, {credentials: "same-origin", cache: "no-store", headers: {accept: "application/json"},
                                signal: ctl.signal});
    let body = null;
    try { body = await r.json(); } catch { body = null; }
    return {ok: r.ok, status: r.status, body};
  } catch {
    return {ok: false, status: 0, body: null};
  } finally {
    clearTimeout(t);
    inflight = null;
  }
}

async function load() {
  if (demo) return demoLoad();
  return fetchJson(API);
}

async function demoLoad() {
  if (demo === "error") return {ok: false, status: 503, body: {detail: "直播服務（8780）目前連不上"}};
  const r = await fetchJson(FIXTURE);
  if (!r.ok) return r;
  if (demo === "empty") return {ok: true, status: 200, body: {latency: {}, asr_samples: 0}};
  if (demo === "stale" && state.lastOk !== null) return {ok: false, status: 0, body: null};
  return r;
}

function apply(action) {
  const prev = state;
  state = reduce(prev, action);
  const g = gateAnnouncement(memo, announcement(prev, state), action.now);
  memo = g.memo;
  if (g.say) $("sr-status").textContent = g.say;
  render();
}

async function tick() {
  timer = null;
  if (stopped || document.hidden) return;
  const r = await load();
  const now = Date.now();
  if (r.ok) apply({type: "ok", body: r.body, now});
  else apply({type: "fail", status: r.status, body: r.body, now});
  if (state.auth) { stopped = true; return; }       // 401：停止輪詢，請使用者重新登入
  schedule(r.ok ? POLL_MS : retryDelay(state.failures - 1));
}

function schedule(ms) {
  if (timer !== null) clearTimeout(timer);
  if (stopped || document.hidden) return;
  timer = setTimeout(tick, ms);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (timer !== null) { clearTimeout(timer); timer = null; }
    if (inflight) inflight.abort();
  } else if (!stopped && timer === null) {
    schedule(0);
  }
});

$("retry").addEventListener("click", () => {
  stopped = false;
  schedule(0);
});

function renderCards(view, failed) {
  const cards = $("cards");
  cards.replaceChildren();
  const rtf = el("article", `card lvl-${view ? view.rtf.level : "none"}`);
  rtf.append(el("h3", "card-h", "辨識速度"));
  if (!view || view.empty || view.rtf.level === "none") {
    rtf.append(el("p", "card-status", failed ? "目前無法取得數字" : "開始聽之後才有數字"));
  } else {
    const st = el("p", "card-status");
    st.append(el("span", "badge", view.rtf.level === "ok" ? "●" : view.rtf.level === "warn" ? "▲" : "■"));
    st.append(document.createTextNode(` ${view.rtf.text}`));
    rtf.append(st);
    const big = el("p", "card-big");
    big.append(el("span", "big-label", "最慢（p95）"), el("b", "", view.rtf.p95Text));
    rtf.append(big);
    rtf.append(el("p", "card-sub", `一般（p50）${view.rtf.p50Text}｜樣本 ${view.rtf.n === null ? "—" : view.rtf.n} 段｜低於 0.9 才跟得上`));
  }
  cards.append(rtf);
  const bl = el("article", "card lvl-neutral");
  bl.append(el("h3", "card-h", "積壓"));
  const blBig = el("p", "card-big");
  blBig.append(el("span", "big-label", "待辨識錄音"), el("b", "", view ? view.backlog : "—"));
  bl.append(blBig);
  bl.append(el("p", "card-sub", `等待辨識 ${view ? view.pending : "—"} 段（已收到、尚未開始辨識的錄音秒數）`));
  cards.append(bl);
}

function renderChart(view, loading) {
  const chart = $("chart");
  chart.replaceChildren();
  if (loading) {
    for (let i = 0; i < 5; i++) chart.append(el("div", "row skeleton"));
    return;
  }
  const scale = el("div", "scale");
  for (const t of view.scale.ticks) {
    const tick = el("span", "tick", t.text);
    tick.style.setProperty("--x", `${t.pct}%`);
    scale.append(tick);
  }
  for (const r of view.rows) {
    const row = el("div", `row lvl-${r.level}`);
    const label = el("div", "row-label");
    label.append(el("b", "", r.id), document.createTextNode(` ${r.name}`));
    row.append(label);
    const track = el("div", "track");
    if (r.p50 !== null || r.p95 !== null) {
      const p95 = el("span", "bar-p95");
      p95.style.setProperty("--w", `${r.p95Pct}%`);
      const p50 = el("span", "bar-p50");
      p50.style.setProperty("--w", `${r.p50Pct}%`);
      track.append(p95, p50);
    }
    if (r.warnInScale) {
      const tgt = el("span", "target");
      tgt.style.setProperty("--x", `${r.warnPct}%`);
      track.append(tgt);
    }
    row.append(track);
    const val = el("div", "row-val");
    if (r.p50 === null && r.p95 === null) val.textContent = "尚無資料";
    else val.textContent = `${r.p50Text}／${r.p95Text}｜${r.levelText}`;
    row.append(val);
    chart.append(row);
  }
  chart.append(scale);
}

function renderTable(view) {
  const tb = $("stage-tbody");
  tb.replaceChildren();
  for (const r of view.rows) {
    const tr = el("tr", `lvl-${r.level}`);
    const th = el("th", "", r.id);
    th.scope = "row";
    tr.append(th, el("td", "", `${r.name}：${r.desc}`),
      el("td", "", r.p50Text), el("td", "", r.p95Text),
      el("td", "", `${(r.warn / 1000).toFixed(2)} s`), el("td", "", r.n === null ? "—" : String(r.n)),
      el("td", "", r.levelText));
    tb.append(tr);
  }
}

function render() {
  const panel = $("panel");
  panel.dataset.phase = state.phase;
  panel.classList.toggle("is-stale", state.stale);
  $("conn").textContent = connText(state, demo);
  const alert = $("alert");
  const showAlert = state.phase === "error" || state.stale;
  alert.hidden = !showAlert;
  alert.classList.toggle("is-stale", state.stale);
  $("alert-text").textContent = state.stale ? `（數字暫停更新）${state.error || ""}` : (state.error || "");
  $("login-link").hidden = !state.auth;
  const loading = state.phase === "loading" || (state.phase === "error" && !state.view);
  const view = state.view || buildView(adaptMetrics(null));
  renderCards(state.view, state.phase === "error" && !state.view);
  const empty = state.view ? state.view.empty : false;
  $("chart-empty").hidden = !empty;
  $("chart").hidden = empty || (state.phase === "error" && !state.view);
  renderChart(view, loading);
  renderTable(view);
}

render();
schedule(0);
