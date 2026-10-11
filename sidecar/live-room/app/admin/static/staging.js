// Staging review page (round3 §5-1). Vanilla ES module, no inline script (CSP script-src 'self').
// Session = HttpOnly cookie; CSRF token held in memory and sent as X-Zen-CSRF on every write,
// same as /admin/static/app.js. Approve / reject / withdraw always carry If-Match (item ETag).
const API = "/admin/api/v1";
const $ = (s, r = document) => r.querySelector(s);
let csrf = null, role = "viewer";
let cursors = [null], page = 0, items = [];

const el = (tag, text, attrs) => {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
};
const banner = (msg, info = false) => { const b = $("#banner"); b.textContent = msg || ""; b.className = info ? "info" : ""; b.hidden = !msg; };
const isAdmin = () => role === "admin" || role === "owner";
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-TW", {timeZone: "Asia/Taipei"}) : "—";
const idemKey = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join("");

class ApiError extends Error { constructor(status, body) { super((body && body.detail) || `HTTP ${status}`); this.status = status; this.body = body; } }

async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const headers = Object.assign({"Accept": "application/json"}, opts.headers || {});
  if (method !== "GET") { headers["X-Zen-CSRF"] = csrf; headers["Content-Type"] = "application/json"; }
  let r;
  try {
    r = await fetch(API + path, {method, headers, credentials: "same-origin",
      body: method !== "GET" ? JSON.stringify(opts.json || {}) : undefined});
  } catch (e) { banner("無法連線到後台服務。"); throw e; }
  let body = null; try { body = await r.json(); } catch (_) { /* empty */ }
  if (r.status === 401) { relogin(); throw new ApiError(401, body); }
  if (!r.ok) {
    const err = new ApiError(r.status, body);
    const hint = {409: "（請重新載入後再處理）", 412: "（資料已被別人改過，已重新載入）", 428: "（缺少 ETag）"}[r.status] || "";
    banner(err.message + hint); throw err;
  }
  return {body, headers: r.headers, status: r.status};
}

function relogin() {
  const m = $("#main"); m.textContent = "";
  m.append(el("p", "需要登入後台。"), el("a", "前往登入頁", {href: "/admin/login"}));
}

function diffCell(it) {
  const td = el("td");
  const before = it.kind === "tm"
    ? (it.base || []).filter(u => u.quality >= 3).map(u => u.tgt_text).join(" ／ ")
    : (it.base ? `${it.base.en}${it.base.locked ? "（鎖定）" : ""}` : "");
  if (before) td.append(el("del", before, {class: "st-old"}), " → ");
  else td.append(el("span", "（新增）"), " ");
  td.append(el("ins", it.tgt_text, {class: "st-new"}));
  if (it.aliases && it.aliases.length) td.append(el("div", "別名：" + it.aliases.join("、")));
  if (it.kind === "term" && it.tgt_lang === "ja") {
    const was = it.base && it.base.reading;
    const r = el("div", "讀音：");
    if (was && was !== it.reading) r.append(el("del", was, {class: "st-old"}), " → ");
    r.append(it.reading ? el("ins", it.reading, {class: "st-new"}) : el("span", was ? "（沿用）" : "（無）"));
    td.append(r);
  }
  if (it.conflict) td.append(el("div", "⚠ 送審後目標已變動", {class: "st-conflict"}));
  if (it.note) td.append(el("div", "備註：" + it.note, {class: "hint"}));
  return td;
}

function button(label, fn) {
  const b = el("button", label, {type: "button"});
  b.onclick = async () => { b.disabled = true; try { await fn(); } catch (e) { if (!(e instanceof ApiError)) banner(String(e)); } finally { b.disabled = false; } };
  return b;
}

async function decide(it, action, json = {}) {
  const r = await api(`/staging/${it.id}/${action}`, {method: "POST", headers: {"If-Match": it.etag}, json});
  banner({approve: "已核准並寫入", reject: "已退回", withdraw: "已撤回"}[action] + `（#${it.id}）`, true);
  return r;
}

function row(it) {
  const tr = el("tr");
  const cb = el("input", null, {type: "checkbox", "aria-label": `勾選 #${it.id}`});
  cb.dataset.id = it.id; cb.disabled = it.state !== "pending" || !isAdmin();
  const c0 = el("td"); c0.append(cb); tr.append(c0);
  tr.append(el("td", it.kind === "term" ? "詞彙" : "翻譯記憶"), el("td", it.src_text), diffCell(it));
  tr.append(el("td", it.state, {class: "st-state-" + it.state}), el("td", fmtTime(it.created_at)));
  const act = el("td", null, {class: "st-actions"});
  act.append(button("明細", () => showDetail(it.id)));
  if (it.state === "pending") {
    if (isAdmin()) {
      act.append(button("核准", async () => {
        const override = it.conflict && confirm("送審後目標已被修改，仍要核准？（會記入稽核）");
        if (it.conflict && !override) return;
        await decide(it, "approve", override ? {override_conflict: true} : {}); await load();
      }));
      act.append(button("退回", async () => {
        const reason = prompt("退回原因（必填）"); if (!reason) return;
        await decide(it, "reject", {reason}); await load();
      }));
    }
    act.append(button("編輯", async () => {
      const tgt = prompt("提議的譯文／譯詞", it.tgt_text); if (tgt === null) return;
      const json = {tgt_text: tgt};
      if (it.kind === "term" && it.tgt_lang === "ja") {
        const rd = prompt("讀音（平假名／片假名；留空 = 不設定）", it.reading || ""); if (rd === null) return;
        json.reading = rd.trim() ? rd.trim() : null;
      }
      await api(`/staging/${it.id}`, {method: "PATCH", headers: {"If-Match": it.etag}, json});
      banner(`已更新 #${it.id}`, true); await load();
    }));
    act.append(button("撤回", async () => { if (!confirm("撤回這筆提議？")) return; await decide(it, "withdraw"); await load(); }));
  }
  tr.append(act);
  return tr;
}

function query() {
  const f = new FormData($("#filters"));
  const p = new URLSearchParams();
  for (const [k, v] of f.entries()) if (v) p.set(k, v);
  if (cursors[page]) p.set("cursor", cursors[page]);
  return p.toString();
}

async function load() {
  try {
    const {body} = await api("/staging?" + query());
    items = body.items;
    const tb = $("#list tbody"); tb.textContent = "";
    if (!items.length) { const tr = el("tr"); tr.append(el("td", "沒有項目", {colspan: "7"})); tb.append(tr); }
    items.forEach(it => tb.append(row(it)));
    cursors[page + 1] = body.next;
    $("#next").disabled = !body.next;
    $("#sel-all").checked = false; updateSel();
  } catch (e) {
    if (e instanceof ApiError && e.status === 400) { cursors = [null]; page = 0; }   // bad/expired cursor
    if (!(e instanceof ApiError)) banner(String(e));
  }
}

function selected() { return [...document.querySelectorAll("#list tbody input[type=checkbox]:checked")].map(c => Number(c.dataset.id)); }
function updateSel() { const n = selected().length; $("#sel-count").textContent = n ? `已勾選 ${n} 筆` : ""; $("#bulk-approve").disabled = !n; }

async function bulkApprove() {
  const ids = new Set(selected());
  const list = items.filter(it => ids.has(it.id)).map(it => ({id: it.id, etag: it.etag}));
  if (!list.length) return;
  const {body} = await api("/staging/bulk-approve", {method: "POST", headers: {"Idempotency-Key": idemKey()},
    json: {items: list, override_conflict: $("#override").checked}});
  const bad = body.results.filter(r => !r.ok).map(r => `#${r.id}：${r.detail}`);
  banner(`核准 ${body.approved} 筆，失敗 ${body.failed} 筆` + (bad.length ? "。" + bad.join("；") : ""), !bad.length);
  await load();
}

async function showDetail(id) {
  const {body: d} = await api(`/staging/${id}`);
  const s = $("#detail"); s.textContent = ""; s.hidden = false;
  s.append(el("h3", `#${d.id} 明細（${d.kind === "term" ? "詞彙" : "翻譯記憶"}・${d.tgt_lang}）`));
  if (d.conflict) s.append(el("p", "⚠ 送審後目標條目已被修改：核准前請比對「目前」與「送審時」。", {class: "st-conflict"}));
  s.append(el("h4", "送審時的目標"), el("pre", JSON.stringify(d.base, null, 2)));
  s.append(el("h4", "目前的目標"), el("pre", JSON.stringify(d.current, null, 2)));
  s.append(el("h4", "提議"), el("pre", JSON.stringify(d.diff.after, null, 2)));
  const t = el("table", null, {"aria-label": "稽核軌跡"});
  const hr = el("tr"); ["時間", "動作", "操作者", "備註"].forEach(h => hr.append(el("th", h, {scope: "col"})));
  t.append(el("thead")); t.tHead.append(hr);
  const tb = el("tbody");
  const modeLabel = {self_auto: "（本人自動核准）", override: "（強制核准）", manual: ""};
  d.audit.forEach(a => { const tr = el("tr"); tr.append(el("td", fmtTime(a.at)), el("td", a.action + (modeLabel[a.mode] || "")), el("td", `${a.actor_via || ""}:${a.actor_id ?? "—"}`), el("td", a.note || "")); tb.append(tr); });
  t.append(tb); s.append(el("h4", "稽核軌跡"), t);
  s.scrollIntoView();
}

$("#filters").addEventListener("submit", (e) => { e.preventDefault(); cursors = [null]; page = 0; load(); });
$("#next").onclick = () => { if (cursors[page + 1]) { page += 1; load(); } };
$("#first").onclick = () => { cursors = [null]; page = 0; load(); };
$("#sel-all").onchange = (e) => { document.querySelectorAll("#list tbody input[type=checkbox]:not(:disabled)").forEach(c => { c.checked = e.target.checked; }); updateSel(); };
$("#list").addEventListener("change", (e) => { if (e.target.matches("tbody input[type=checkbox]")) updateSel(); });
$("#bulk-approve").onclick = () => bulkApprove().catch(e => { if (!(e instanceof ApiError)) banner(String(e)); });

(async () => {
  try {
    const {body: me} = await api("/auth/session");
    csrf = me.csrf; role = me.role; $("#who").textContent = `角色：${me.role}`;
    $("#bulkbar").hidden = !isAdmin();
    await load();
  } catch (e) { if (!(e instanceof ApiError)) banner(String(e)); }
})();
