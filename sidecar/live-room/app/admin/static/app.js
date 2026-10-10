// Vanilla JS module. Session = HttpOnly cookie; the CSRF token is kept in memory only. No inline script (CSP).
import {backoff, fmtBytes, fmtMs, isStale, pageFromHash, parseSse, problemMessage, rtfColor, workbenchKey} from "/admin/static/admin_logic.js";

const API = "/admin/api/v1";
let csrf = null, role = "viewer";
const $ = (s, r = document) => r.querySelector(s);
const el = (tag, text, attrs) => {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
};
const banner = (msg, kind = "error") => {
  const b = $("#banner"); b.textContent = msg; b.className = kind === "info" ? "info" : ""; b.hidden = !msg;
};

class ApiError extends Error { constructor(status, body) { super(problemMessage(status, body)); this.status = status; this.body = body; } }

function showRelogin() {
  const p = $("#page"); p.textContent = "";
  p.append(el("p", problemMessage(401)));
  const a = el("a", "前往登入頁", {href: "/admin/login"}); p.append(a); a.focus();
}

async function api(path, opts = {}) {
  const headers = Object.assign({"Accept": "application/json"}, opts.headers || {});
  const method = (opts.method || "GET").toUpperCase();
  if (method !== "GET") {
    headers["X-Zen-CSRF"] = csrf;
    if (opts.json !== undefined) headers["Content-Type"] = "application/json";
  }
  let r;
  try {
    r = await fetch(API + path, {method, headers, credentials: "same-origin",
      body: opts.json !== undefined ? JSON.stringify(opts.json) : undefined});
  } catch (e) { banner("無法連線到後台服務（127.0.0.1:8791）。"); throw e; }
  if (r.status === 401) { showRelogin(); throw new ApiError(401, null); }
  if (!r.ok) {
    let body = null; try { body = await r.json(); } catch (_) { /* not json */ }
    const err = new ApiError(r.status, body); banner(err.message); throw err;
  }
  return r;
}
const getJson = async (p) => (await api(p)).json();
const can = (need) => ({viewer: 0, host: 1, editor: 2, admin: 3, owner: 4}[role] >= {viewer: 0, editor: 2, admin: 3}[need]);

function table(headers, rows, label) {
  const t = el("table", null, {"aria-label": label});
  const tr = el("tr"); headers.forEach(h => tr.append(el("th", h, {scope: "col"}))); t.append(el("thead")); t.tHead.append(tr);
  const tb = el("tbody");
  for (const r of rows) { const row = el("tr"); r.forEach(c => { const td = el("td"); if (c instanceof Node) td.append(c); else td.textContent = c ?? ""; row.append(td); }); tb.append(row); }
  t.append(tb); return t;
}
const btn = (label, fn, aria) => { const b = el("button", label, {type: "button", "aria-label": aria || label}); b.onclick = async () => { try { b.disabled = true; await fn(); } catch (e) { if (!(e instanceof ApiError)) banner(String(e)); } finally { b.disabled = false; } }; return b; };
const card = (title, value, cls = "") => { const c = el("div", null, {class: "card " + cls, role: "group", "aria-label": title}); c.append(el("span", title), el("b", value)); return c; };
const section = (title) => { const s = el("section", null, {"aria-label": title}); s.append(el("h3", title)); $("#page").append(s); return s; };
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-TW", {timeZone: "Asia/Taipei"}) : "—";

// ---------------------------------------------------------------- pages
const pages = {};
let stopPage = () => {};

pages.overview = async () => {
  const [ov, inv, hw, pipe, feed] = await Promise.all([getJson("/overview"), getJson("/overview/inventory"),
    getJson("/overview/hardware"), getJson("/overview/pipeline"), getJson("/overview/feed")]);
  const s1 = section("系統元件");
  const g = el("div", null, {class: "grid"});
  for (const [name, v] of Object.entries(ov.components)) g.append(card(name, v.status + (v.uptime_s ? `（已運行 ${Math.round(v.uptime_s)} 秒）` : ""), v.status === "up" || v.status === "ok" ? "green" : "red"));
  g.append(card("RTF", ov.rtf ?? "—", rtfColor(ov.rtf) + (ov.stale ? " stale" : "")));
  s1.append(g);
  if (ov.stale) s1.append(el("p", "直播資料已過期（超過 10 秒沒有更新）"));
  const s2 = section("硬體資源");
  if (!hw.available) s2.append(el("p", "psutil 不可用，無法取得硬體資訊。"));
  else {
    s2.append(el("p", `CPU 每核：${(hw.cpu_per_core || []).map(x => x + "%").join("、")}；RAM ${hw.ram ? hw.ram.percent + "%" : "—"}；電源：${hw.power ? (hw.power.ac ? "插電" : "電池 " + hw.power.battery_pct + "%") : "—"}`));
    s2.append(table(["磁碟", "可用", "使用率"], Object.entries(hw.disks || {}).map(([k, d]) => [k, d ? fmtBytes(d.free) : "—", d ? d.percent + "%" : "—"]), "磁碟空間"));
    s2.append(table(["PID", "角色", "記憶體"], (hw.processes || []).map(p => [p.pid, p.role, fmtBytes(p.rss)]), "程序"));
  }
  const s3 = section("管線遙測");
  s3.append(el("p", `stale ${pipe.counts.stale}、skipped ${pipe.counts.skipped}、merged ${pipe.counts.merged}、鎖定詞違規 ${pipe.counts.locked_term_violations}、重複防護 ${pipe.asr.repetition_guard_hits}、ASR 平均 RTF ${pipe.asr.rtf_avg ?? "—"}`));
  s3.append(table(["段落", "收到", "ASR ms", "翻譯 ms", "狀態"], pipe.segments.slice(0, 20).map(r => [r.segment_id, fmtTime(r.received_at), r.asr_ms ?? "—", r.latency_ms ?? "—", r.status]), "最近段落時間軸"));
  const s4 = section("資料盤點");
  const g4 = el("div", null, {class: "grid"});
  for (const [k, v] of Object.entries(inv.counts)) g4.append(card(k, v));
  for (const [k, v] of Object.entries(inv.sizes)) g4.append(card(k, fmtBytes(v)));
  g4.append(card("最近備份", inv.backups.last ? `${Math.round(inv.backups.last_age_s / 3600)} 小時前${inv.backups.manifest_present ? "（有 manifest）" : ""}` : "沒有備份", inv.backups.last ? "" : "red"));
  s4.append(g4);
  const s5 = section("事件與錯誤");
  s5.append(table(["時間", "種類", "等級", "場次"], feed.events.slice(0, 30).map(e => [fmtTime(e.ts), e.kind, e.level, e.session_id]), "事件"));
  s5.append(table(["工作", "種類", "狀態"], feed.jobs.slice(0, 15).map(j => [j.id, j.kind, j.state]), "工作歷史"));
  const exp = section("指標匯出");
  exp.append(el("a", "下載 CSV", {href: API + "/metrics/export?format=csv", download: "metrics.csv"}), el("span", " "),
    el("a", "下載 JSON", {href: API + "/metrics/export?format=json"}));
};

pages.monitor = async () => {
  const s = section("即時指標");
  const status = el("p", "連線中…", {"aria-live": "polite"}); s.append(status);
  const cards = el("div", null, {class: "grid"}); s.append(cards);
  const errs = section("錯誤動態"); const list = el("ol"); errs.append(list);
  const timings = await getJson("/overview/asr-timings");
  section("ASR 每段計時").append(table(["音訊秒", "ASR 秒", "RTF", "重試"], timings.clips.slice(-20).map(c => [c.audio_s, c.asr_s, c.rtf, c.retried_full_ctx ? "是" : ""]), "ASR 計時"));
  let es = null, attempt = 0, lastTs = null, timer = null, alive = true;
  const staleTimer = setInterval(() => { if (isStale(lastTs, Date.now() / 1000)) cards.classList.add("stale"); }, 2000);
  const connect = () => {
    if (!alive) return;
    es = new EventSource(API + "/monitor/stream");
    es.addEventListener("metrics", (e) => {
      attempt = 0; const d = JSON.parse(e.data); lastTs = Date.now() / 1000;
      status.textContent = d.stale ? "直播資料過期或直播服務未啟動" : "即時更新中";
      cards.textContent = ""; cards.classList.toggle("stale", !!d.stale);
      const m = d.metrics || {};
      cards.append(card("RTF", m.rtf ?? "—", d.rtf_color), card("佇列", m.pending ?? "—"), card("處理中", m.inflight ?? "—"),
        card("翻譯排隊", m.translate_queued ?? "—"), card("略過", m.translate_skipped ?? "—"), card("合併", m.translate_merged ?? "—"),
        card("延遲(秒)", m.backlog_s ?? "—"), card("聽眾", m.listeners ?? "—"));
    });
    es.addEventListener("errors", (e) => { list.textContent = ""; for (const x of JSON.parse(e.data)) list.append(el("li", `${fmtTime(x.ts)} ${x.kind}`)); });
    es.addEventListener("logout", () => { es.close(); showRelogin(); });
    es.onerror = async () => {
      es.close();
      try { await api("/auth/session"); } catch (e) { if (e.status === 401) return; }
      const wait = backoff(attempt++); status.textContent = `連線中斷，${Math.round(wait / 1000)} 秒後重試…`;
      timer = setTimeout(connect, wait);
    };
  };
  connect();
  stopPage = () => { alive = false; clearInterval(staleTimer); clearTimeout(timer); if (es) es.close(); };
};

pages.sessions = async () => {
  const data = await getJson("/sessions?limit=100");
  const s = section("場次列表");
  if (can("editor")) {
    const f = el("form", null, {"aria-label": "建立場次"});
    const room = el("input", null, {name: "room", required: "", "aria-label": "房間 id", placeholder: "房間 id"});
    const title = el("input", null, {name: "title", "aria-label": "標題", placeholder: "標題"});
    const lang = el("select", null, {"aria-label": "目標語言"}); ["en", "ja"].forEach(v => lang.append(el("option", v === "en" ? "英文" : "日文", {value: v})));
    f.append(room, title, lang, el("button", "建立", {type: "submit"}));
    f.onsubmit = async (ev) => { ev.preventDefault(); await api("/sessions", {method: "POST", json: {room_id: room.value, title: title.value || undefined, tgt_lang: lang.value}}); render(); };
    s.append(f);
  }
  s.append(table(["開始", "房間", "標題", "狀態", "段數", ""], data.items.map(x => {
    const ops = el("span");
    ops.append(btn("詳情", () => drill(x.id), `場次 ${x.id} 詳情`));
    if (can("editor") && x.status === "live") ops.append(btn("結束", async () => { await api(`/sessions/${encodeURIComponent(x.id)}/close`, {method: "POST"}); render(); }, `結束場次 ${x.id}`));
    if (can("admin")) ops.append(btn("刪除", async () => {
      const pv = await (await api(`/sessions/${encodeURIComponent(x.id)}/delete-preview`, {method: "POST"})).json();
      const typed = prompt(`將刪除 ${JSON.stringify(pv.counts)}。請輸入場次 id 確認：`);
      if (typed !== x.id) return;
      await api(`/sessions/${encodeURIComponent(x.id)}`, {method: "DELETE", json: {confirm: x.id}}); banner("已刪除", "info"); render();
    }, `刪除場次 ${x.id}`));
    return [fmtTime(x.started_at), x.room_id, x.title, x.status, x.segments, ops];
  }), "場次"));
  const ret = await getJson("/retention");
  const r = section("保留政策");
  r.append(el("p", `到期待刪 ${ret.sessions_due} 場；法律保全 ${ret.legal_hold} 場；自動整場刪除${ret.purge_enabled ? "啟用（24 小時內有備份）" : "暫停（沒有 24 小時內的備份）"}`));
  r.append(table(["項目", "保留天數", "說明", ""], ret.items.map(i => {
    const inp = el("input", null, {type: "number", min: "1", value: i.keep_days ?? "", "aria-label": `${i.item} 保留天數`, size: "5"});
    return [i.item, can("admin") ? inp : (i.keep_days ?? "永久"), i.note, can("admin") ? btn("儲存", async () => { await api(`/retention/${i.item}`, {method: "PUT", json: {keep_days: inp.value ? Number(inp.value) : null}}); banner("已儲存", "info"); }, `儲存 ${i.item}`) : ""];
  }), "保留政策"));
};

async function drill(sid) {
  location.hash = "#review/" + encodeURIComponent(sid);
}

pages.review = async () => {
  const sid = decodeURIComponent(location.hash.split("/")[1] || "");
  const s = section("審稿工作台");
  s.append(el("p", "鍵盤：j/k 或 ↑/↓ 移動、Enter 編輯、t 切換中文/譯文、Ctrl+Enter 儲存、Esc 取消、Ctrl+Z 復原、p 推送到房間"));
  if (!sid) { s.append(el("p", "請先到「場次」選擇一個場次。")); return; }
  const d = await getJson(`/sessions/${encodeURIComponent(sid)}/drilldown`);
  const lang = d.session.tgt_lang;
  s.append(el("p", `場次 ${sid}｜目標語言 ${lang}｜平均 ASR ${d.timings.asr_ms_avg ?? "—"} ms｜平均翻譯 ${d.timings.mt_ms_avg ?? "—"} ms｜RTF ${d.timings.rtf_avg ?? "—"}`));
  const t = table(["#", "時間", "中文", "譯文", "來源"], d.segments.map(g => [g.seq, fmtMs(g.t0_ms), g.zh, g.tgt, g.tgt_origin]), "段落");
  t.tabIndex = 0; s.append(t);
  const rows = [...t.tBodies[0].rows];
  let idx = 0, target = "zh", editing = null;
  const select = (i) => { rows.forEach(r => r.classList.remove("sel")); idx = i; if (rows[i]) { rows[i].classList.add("sel"); rows[i].setAttribute("aria-selected", "true"); rows[i].scrollIntoView({block: "nearest"}); } };
  const cellOf = () => rows[idx].cells[target === "zh" ? 2 : 3];
  const segId = () => d.segments[idx].id;
  const tgtKey = () => target === "zh" ? "zh" : lang;
  async function etag() { const h = await getJson(`/segments/${encodeURIComponent(segId())}/history`); return h.etags[tgtKey()] || '"v0"'; }
  t.addEventListener("keydown", async (ev) => {
    const a = workbenchKey({key: ev.key, ctrlKey: ev.ctrlKey, metaKey: ev.metaKey, editing: !!editing}, idx, rows.length);
    if (a.action === "none") return;
    ev.preventDefault();
    try {
      if (a.action === "move") select(a.index);
      else if (a.action === "toggle-target") { target = target === "zh" ? "tgt" : "zh"; banner(`編輯目標：${target === "zh" ? "中文" : "譯文"}`, "info"); }
      else if (a.action === "edit" && rows[idx]) {
        const c = cellOf(); const ta = el("textarea", null, {"aria-label": target === "zh" ? "中文修正" : "譯文修正"}); ta.value = c.textContent;
        editing = {cell: c, old: c.textContent, ta}; c.textContent = ""; c.append(ta); ta.focus();
      } else if (a.action === "cancel" && editing) { editing.cell.textContent = editing.old; editing = null; t.focus(); }
      else if (a.action === "save" && editing) {
        const r = await (await api(`/segments/${encodeURIComponent(segId())}/text`, {method: "PATCH", headers: {"If-Match": await etag()}, json: {target: tgtKey(), text: editing.ta.value}})).json();
        editing.cell.textContent = editing.ta.value; editing = null; t.focus();
        banner(`已存成第 ${r.version} 版${r.tm_pending ? "；翻譯記憶待管理員核准" : ""}`, "info");
      } else if (a.action === "undo") {
        const r = await (await api(`/segments/${encodeURIComponent(segId())}/undo`, {method: "POST", headers: {"If-Match": await etag()}, json: {target: tgtKey()}})).json();
        banner(`已復原到第 ${r.version} 版`, "info"); render();
      } else if (a.action === "push") { await api(`/segments/${encodeURIComponent(segId())}/push`, {method: "POST"}); banner("已推送到房間（未重新翻譯）", "info"); }
    } catch (e) { if (!(e instanceof ApiError)) banner(String(e)); }
  });
  select(0); t.focus();
  section("修正紀錄").append(table(["段落", "類型", "原文", "修正", "狀態"], d.corrections.map(c => [c.segment_id, c.target_type, c.before_text, c.after_text, c.status]), "修正"));
  section("錯誤").append(table(["時間", "種類", "段落"], d.errors.map(e => [fmtTime(e.ts), e.kind, e.segment_id]), "錯誤"));
  if (can("editor")) {
    const q = await getJson("/review/queue");
    section("待核准的翻譯記憶").append(table(["中文", "譯文", ""], q.tm_pending.map(u => [u.src_text, u.tgt_text, can("admin") ? el("span") : "等待管理員"]), "待核准"));
    if (can("admin")) [...$("#page").querySelectorAll("table[aria-label=待核准] tbody tr")].forEach((tr, i) => {
      const u = q.tm_pending[i]; const span = tr.cells[2].firstChild;
      span.append(btn("核准", async () => { await api(`/tm/${u.id}/approve`, {method: "POST"}); render(); }, `核准 ${u.src_text}`),
        btn("駁回", async () => { await api(`/tm/${u.id}/reject`, {method: "POST"}); render(); }, `駁回 ${u.src_text}`));
    });
    // round3 §5-1: corrections wait in staging; only an admin's approval writes TM / glossary.
    const st = q.staging_pending || [];
    section("待審修正（staging）").append(table(["類型", "中文", "提議", ""], st.map(u => [u.kind === "term" ? "詞彙" : "翻譯記憶", u.src_text, u.tgt_text, can("admin") ? el("span") : "等待管理員"]), "待審修正"));
    if (can("admin")) [...$("#page").querySelectorAll("table[aria-label=待審修正] tbody tr")].forEach((tr, i) => {
      const u = st[i]; const span = tr.cells[3].firstChild;
      span.append(btn("核准", async () => { await api(`/staging/${u.id}/approve`, {method: "POST", headers: {"If-Match": u.etag}, json: {}}); render(); }, `核准 ${u.src_text}`),
        btn("退回", async () => { const reason = prompt("退回原因"); if (!reason) return; await api(`/staging/${u.id}/reject`, {method: "POST", headers: {"If-Match": u.etag}, json: {reason}}); render(); }, `退回 ${u.src_text}`));
    });
  }
};

pages.glossary = async () => {
  const lang = sessionStorage.getItem("gl-lang") || "en";
  const s = section(`詞表（${lang === "en" ? "英文" : "日文"}）`);
  const sw = el("select", null, {"aria-label": "詞表語言"}); ["en", "ja"].forEach(v => { const o = el("option", v === "en" ? "英文" : "日文", {value: v}); if (v === lang) o.selected = true; sw.append(o); });
  sw.onchange = () => { sessionStorage.setItem("gl-lang", sw.value); render(); }; s.append(sw);
  const [terms, props] = await Promise.all([getJson(`/glossary/terms?lang=${lang}`), getJson(`/glossary/terms?lang=${lang}&status=proposed`)]);
  if (can("editor")) {
    const f = el("form", null, {"aria-label": "新增詞條"});
    const zh = el("input", null, {"aria-label": "中文", placeholder: "中文", required: ""}), tg = el("input", null, {"aria-label": "譯詞", placeholder: "譯詞", required: ""});
    const rd = el("input", null, {"aria-label": "讀音（日文）", placeholder: "讀音"}); const al = el("input", null, {"aria-label": "別名（以 | 分隔）", placeholder: "別名 a|b"});
    f.append(zh, tg); if (lang === "ja") f.append(rd); f.append(al, el("button", "新增", {type: "submit"}));
    f.onsubmit = async (ev) => { ev.preventDefault(); await api("/glossary/terms", {method: "POST", json: {zh: zh.value, target: tg.value, tgt_lang: lang, reading: rd.value || undefined, aliases: al.value ? al.value.split("|") : []}}); render(); };
    s.append(f);
  }
  s.append(table(["中文", "譯詞", "讀音", "別名", "鎖定", ""], terms.items.map(t => [t.zh, t.en, t.reading, t.aliases.join("、"), t.locked ? "🔒" : "",
    can("editor") ? (() => { const sp = el("span");
      sp.append(btn(t.locked ? "解鎖" : "鎖定", async () => { await api(`/glossary/terms/${t.id}`, {method: "PATCH", headers: {"If-Match": t.etag}, json: {locked: !t.locked}}); render(); }, `${t.locked ? "解鎖" : "鎖定"} ${t.zh}`));
      sp.append(btn("刪除", async () => { await api(`/glossary/terms/${t.id}`, {method: "DELETE", headers: {"If-Match": t.etag}}); render(); }, `刪除 ${t.zh}`));
      return sp; })() : ""]), "詞條"));
  const ps = section("建議詞審核");
  ps.append(table(["中文", "譯詞", "次數", ""], props.items.map(p => [p.zh, p.en, p.hit_count, can("editor") ? (() => { const sp = el("span");
    sp.append(btn("通過", async () => { await api(`/glossary/proposals/${p.id}/approve`, {method: "POST", headers: {"If-Match": p.etag}}); render(); }, `通過 ${p.zh}`),
      btn("駁回", async () => { await api(`/glossary/proposals/${p.id}/reject`, {method: "POST", headers: {"If-Match": p.etag}}); render(); }, `駁回 ${p.zh}`),
      btn("併為別名", async () => { const into = prompt("併入哪個詞條 id？"); if (!into) return; await api(`/glossary/proposals/${p.id}/merge`, {method: "POST", headers: {"If-Match": p.etag}, json: {into_term_id: Number(into)}}); render(); }, `把 ${p.zh} 併為別名`));
    return sp; })() : ""]), "建議詞"));
  const io = section("匯入／匯出／推送");
  io.append(el("a", "匯出 CSV", {href: `${API}/glossary/export.csv?lang=${lang}`, download: ""}));
  if (can("editor")) {
    const ta = el("textarea", null, {"aria-label": "CSV 內容（zh,target,reading,aliases）"}); io.append(ta);
    io.append(btn("預覽匯入", async () => { const r = await (await api("/glossary/import", {method: "POST", json: {csv: ta.value, tgt_lang: lang, dry_run: true}})).json(); banner(`新增 ${r.counts.add}、更新 ${r.counts.update}、錯誤 ${r.counts.errors}`, "info"); }),
      btn("匯入", async () => { await api("/glossary/import", {method: "POST", json: {csv: ta.value, tgt_lang: lang}}); render(); }));
    if (lang === "en") {
      const room = el("input", null, {"aria-label": "推送到房間 id", placeholder: "房間 id"}); io.append(room);
      const out = el("pre", null, {"aria-live": "polite"});
      let seen = null;
      io.append(btn("預覽差異", async () => { const r = await (await api(`/rooms/${encodeURIComponent(room.value)}/glossary/push`, {method: "POST", json: {dry_run: true}})).json(); seen = r.version; out.textContent = JSON.stringify(r, null, 1); }),
        btn("推送", async () => { if (seen === null) { banner("請先預覽差異"); return; } await api(`/rooms/${encodeURIComponent(room.value)}/glossary/push`, {method: "POST", json: {if_room_version: seen}}); banner("已推送", "info"); }), out);
    }
  }
};

pages.tm = async () => {
  const s = section("翻譯記憶");
  const q = el("input", null, {"aria-label": "搜尋翻譯記憶", placeholder: "搜尋"}); const st = el("select", null, {"aria-label": "狀態"});
  [["", "全部"], ["live", "使用中"], ["pending", "待核准"], ["disabled", "停用"]].forEach(([v, l]) => st.append(el("option", l, {value: v})));
  const box = el("div"); s.append(q, st, btn("搜尋", load), box);
  async function load() {
    const d = await getJson(`/tm?q=${encodeURIComponent(q.value)}&status=${st.value}`); box.textContent = "";
    box.append(table(["中文", "譯文", "狀態", "來源", ""], d.items.map(u => [u.src_text, u.tgt_text, u.state, `${u.origin} ${u.segment_id || ""}`, (() => { const sp = el("span");
      if (can("editor")) sp.append(btn("編輯", async () => { const v = prompt("新的譯文", u.tgt_text); if (!v) return; await api(`/tm/${u.id}`, {method: "PATCH", headers: {"If-Match": u.etag}, json: {tgt_text: v}}); load(); }, `編輯 ${u.src_text}`),
        btn("停用", async () => { await api(`/tm/${u.id}/disable`, {method: "POST"}); load(); }, `停用 ${u.src_text}`));
      return sp; })()]), "翻譯記憶"));
  }
  await load();
};

pages.search = async () => {
  const s = section("全文搜尋");
  const f = el("form", null, {role: "search", "aria-label": "搜尋字幕"});
  const q = el("input", null, {"aria-label": "關鍵字", maxlength: "200", required: "", placeholder: "1–2 字用單字索引、3 字以上用 trigram"});
  const lang = el("select", null, {"aria-label": "語言"}); [["zh", "中文"], ["en", "英文"], ["ja", "日文"]].forEach(([v, l]) => lang.append(el("option", l, {value: v})));
  const sess = el("input", null, {"aria-label": "場次 id（選填）", placeholder: "場次 id"});
  const from = el("input", null, {type: "date", "aria-label": "起始日期"}), to = el("input", null, {type: "date", "aria-label": "結束日期"});
  const out = el("ol", null, {"aria-live": "polite"});
  f.append(q, lang, sess, from, to, el("button", "搜尋", {type: "submit"})); s.append(f, out);
  f.onsubmit = async (ev) => { ev.preventDefault();
    const p = new URLSearchParams({q: q.value, lang: lang.value});
    if (sess.value) p.set("in_session", sess.value);
    if (from.value) p.set("date_from", String(Date.parse(from.value + "T00:00:00+08:00") / 1000));
    if (to.value) p.set("date_to", String(Date.parse(to.value + "T23:59:59+08:00") / 1000));
    const d = await getJson("/search?" + p); out.textContent = "";
    for (const h of d.items) { const li = el("li"); const a = el("a", `${h.session_id} @${fmtMs(h.t0_ms)}`, {href: `#review/${encodeURIComponent(h.session_id)}`}); li.append(a, el("span", " " + h.snippet)); out.append(li); }
    if (!d.items.length) out.append(el("li", "沒有結果"));
    banner(`模式：${d.mode}`, "info");
  };
  q.focus();
};

pages.exports = async () => {
  const s = section("匯出（SRT / VTT / Markdown）");
  s.append(el("p", "本機沒有 python-docx，暫不提供 DOCX；Markdown 可用 Word 開啟。"));
  const sess = el("input", null, {"aria-label": "場次 id", placeholder: "場次 id"}); const fmt = el("select", null, {"aria-label": "格式"}); ["srt", "vtt", "md"].forEach(v => fmt.append(el("option", v, {value: v})));
  const variant = el("select", null, {"aria-label": "內容"}); [["bilingual", "雙語"], ["zh", "只有中文"], ["en", "只有譯文"]].forEach(([v, l]) => variant.append(el("option", l, {value: v})));
  s.append(sess, fmt, variant);
  if (can("editor")) s.append(btn("建立匯出工作", async () => { const r = await (await api(`/sessions/${encodeURIComponent(sess.value)}/exports`, {method: "POST", json: {format: fmt.value, variant: variant.value}})).json(); banner(`已排入工作 ${r.job.id}`, "info"); }));
  const d = await getJson("/exports");
  s.append(table(["檔案", "大小", "時間"], d.items.map(f => [el("a", f.file, {href: `${API}/exports/file/${encodeURIComponent(f.file)}`, download: f.file}), fmtBytes(f.bytes), fmtTime(f.mtime)]), "匯出檔案"));
};

pages.config = async () => {
  const [cfg, health] = await Promise.all([getJson("/config"), getJson("/models/health")]);
  const s = section("服務健康");
  const g = el("div", null, {class: "grid"}); for (const [k, v] of Object.entries(health)) g.append(card(k, v, v === "up" ? "green" : v === "unknown" ? "unknown" : "red")); s.append(g);
  const p = section("翻譯引擎設定檔與目標語言");
  p.append(el("p", cfg.read_only_note));
  if (can("admin")) {
    const sel = el("select", null, {"aria-label": "翻譯引擎設定檔"}); cfg.profiles.forEach(x => { const o = el("option", `${x.name}（${x.tier}）`, {value: x.name}); if (x.is_active) o.selected = true; sel.append(o); });
    const lang = el("select", null, {"aria-label": "預設目標語言"}); ["en", "ja"].forEach(v => { const o = el("option", v, {value: v}); if (cfg.settings.default_tgt_lang === v) o.selected = true; lang.append(o); });
    p.append(sel, btn("套用設定檔", async () => { const r = await (await api("/config/translate_profile", {method: "PUT", json: {value: sel.value}})).json(); banner(r.restart_required ? "已套用，需重新啟動翻譯服務" : "已套用", "info"); }),
      lang, btn("設定目標語言", async () => { await api("/config/default_tgt_lang", {method: "PUT", json: {value: lang.value}}); banner("已設定", "info"); }));
  }
  section("有效設定（機密已遮蔽）").append(el("pre", JSON.stringify({env: cfg.env, profiles: cfg.profiles, settings: cfg.settings, extra: cfg.extra}, null, 1)));
};

pages.audit = async () => {
  if (!can("admin")) { section("稽核紀錄").append(el("p", problemMessage(403, {detail: "需要管理員"}))); return; }
  const d = await getJson("/audit?limit=200");
  section("稽核紀錄").append(table(["時間", "動作", "操作者", "場次", "內容"], d.items.map(e => [fmtTime(e.ts), e.kind.replace(/^audit\./, ""), e.actor_id ?? (e.payload && e.payload.via), e.session_id, JSON.stringify(e.payload)]), "稽核"));
};

pages.users = async () => {
  if (!can("admin")) { section("使用者").append(el("p", problemMessage(403, {detail: "需要管理員"}))); return; }
  const d = await getJson("/users");
  const s = section("使用者與角色");
  const f = el("form", null, {"aria-label": "新增使用者"});
  const u = el("input", null, {"aria-label": "帳號", required: "", placeholder: "帳號"}); const r = el("select", null, {"aria-label": "角色"}); ["viewer", "editor", "admin"].forEach(v => r.append(el("option", v, {value: v})));
  f.append(u, r, el("button", "新增", {type: "submit"})); f.onsubmit = async (ev) => { ev.preventDefault(); await api("/users", {method: "POST", json: {username: u.value, role: r.value}}); render(); };
  s.append(f);
  const out = el("pre", null, {"aria-live": "polite"});
  s.append(table(["id", "帳號", "角色", "停用", "權杖", ""], d.items.map(x => [x.id, x.username, x.role, x.disabled ? "是" : "", x.tokens, (() => { const sp = el("span");
    sp.append(btn(x.disabled ? "啟用" : "停用", async () => { await api(`/users/${x.id}`, {method: "PATCH", json: {disabled: !x.disabled}}); render(); }, `${x.disabled ? "啟用" : "停用"} ${x.username}`),
      btn("發權杖", async () => { const t = await (await api(`/users/${x.id}/tokens`, {method: "POST", json: {scopes: x.role === "viewer" ? "read" : "read,write"}})).json(); out.textContent = `權杖只顯示這一次：${t.token}`; }, `替 ${x.username} 發權杖`));
    return sp; })()]), "使用者"), out);
};

// ---------------------------------------------------------------- router
async function render() {
  stopPage(); stopPage = () => {};
  banner("");
  const name = pageFromHash(location.hash);
  for (const a of document.querySelectorAll("#nav a")) { if (a.getAttribute("href") === "#" + name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); }
  $("#page-title").textContent = $(`#nav a[href="#${name}"]`).textContent;
  $("#page").textContent = "";
  try { await pages[name](); } catch (e) { if (!(e instanceof ApiError)) banner("載入失敗：" + e); }
  $("#main").focus();
}
window.addEventListener("hashchange", render);

$("#logout").onclick = async () => { try { await api("/auth/logout", {method: "POST"}); } finally { location.replace("/admin/login"); } };

(async () => {
  try {
    const me = await getJson("/auth/session");
    csrf = me.csrf; role = me.role; $("#who").textContent = `角色：${me.role}`;
    render();
  } catch (_) { /* 401 handled */ }
})();
