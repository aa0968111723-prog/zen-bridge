// 訓練資料頁（/admin/training）。Vanilla ES module，無 inline script（CSP script-src 'self'）。
// Cookie session + X-Zen-CSRF，與 /admin、/admin/staging 相同。
import {encodeWav, FLAG_ZH, fmtHours, fmtMinutes, level, meterPct, precheck, resample, reviewKey, SOURCE_ZH, STATUS_ZH, TARGET_RATE} from "./training_logic.js";

const API = "/admin/api/v1";
const T = API + "/training";
const $ = (s, r = document) => r.querySelector(s);
const TAIL_MS = 1200;              // 停止後多收 1.2 秒尾音
const IDLE_WARN_MS = 4 * 60e3, IDLE_STOP_MS = 5 * 60e3;
let csrf = null, speakers = [], lastOverview = null;

const el = (tag, text, attrs) => {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
};
const banner = (msg, info = false) => { const b = $("#banner"); b.textContent = msg || ""; b.className = info ? "info" : ""; b.hidden = !msg; };
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-TW", {timeZone: "Asia/Taipei"}) : "—";

async function api(path, {method = "GET", json, body, headers = {}} = {}) {
  const h = {Accept: "application/json", ...headers};
  if (method !== "GET") h["X-Zen-CSRF"] = csrf || "";
  if (json !== undefined) { h["Content-Type"] = "application/json"; body = JSON.stringify(json); }
  let r;
  try { r = await fetch(path, {method, headers: h, body, credentials: "same-origin"}); }
  catch (e) { banner("無法連線到後台服務。"); throw e; }
  let data = null; try { data = await r.json(); } catch (_) { /* empty */ }
  if (r.status === 401) { $("#main").replaceChildren(el("p", "需要登入後台。"), el("a", "前往登入頁", {href: "/admin/login"})); throw new Error("401"); }
  if (!r.ok) { const m = (data && data.detail) || `HTTP ${r.status}`; banner(m); throw new Error(m); }
  return data;
}
const btn = (label, fn, attrs) => { const b = el("button", label, {type: "button", ...attrs}); b.onclick = async () => { b.disabled = true; try { await fn(); } catch (_) { /* banner shown */ } finally { b.disabled = false; } }; return b; };

// ---------------------------------------------------------------- tabs
for (const b of document.querySelectorAll(".tr-tabs button")) {
  b.onclick = () => {
    for (const x of document.querySelectorAll(".tr-tabs button")) x.setAttribute("aria-selected", String(x === b));
    for (const p of document.querySelectorAll(".tr-pane")) p.hidden = p.id !== "tab-" + b.dataset.tab;
    ({review: loadReview, recordings: loadRecordings, export: loadExports, harvest: loadHarvest, classreview: loadClassList,
      hours: loadHours, settings: loadSettings}[b.dataset.tab] || (() => {}))();
  };
}

// ---------------------------------------------------------------- speakers + consent
async function loadOverview() {
  const o = await api(T);
  speakers = o.speakers;
  $("#root").textContent = o.root;
  $("#consent-text").textContent = o.consent_text;
  $("#quick-consent-text").textContent = o.consent_text;
  lastOverview = o;
  const open = (o.queue.unverified || 0) + (o.queue.needs_review || 0);
  $("#q-count").textContent = open ? `(${open})` : "";
  const tb = $("#spk-table tbody"); tb.textContent = "";
  for (const s of speakers) {
    const c = s.consent;
    const scope = c ? [c.scope_asr && "ASR", c.scope_mt && "MT", c.harvest_live && "現場收錄"].filter(Boolean).join("、") + `（${c.consent_date}）` : "尚未同意";
    const tr = el("tr");
    tr.append(el("td", s.label), el("td", s.id), el("td", scope, {class: c ? "tr-ok" : "tr-warn"}), el("td", fmtMinutes(s.minutes_total)));
    const td = el("td");
    td.append(btn(c ? "更新同意" : "記錄同意", () => openConsent(s)), " ",
      btn("撤回並刪除", async () => {
        if (!confirm(`確定撤回「${s.label}」？他所有的錄音、逐字稿片段、與包含他的匯出都會被刪除，無法復原。`)) return;
        const r = await api(`${T}/speakers/${s.id}/withdraw`, {method: "POST", json: {confirm: true}});
        banner(`已撤回並刪除：${r.clips_deleted} 個片段、${r.exports_deleted} 份匯出、${r.files_deleted} 個檔案`, true);
        await loadOverview();
      }));
    tr.append(td); tb.append(tr);
  }
  for (const sel of [$("#studio-spk"), $("#harvest-spk")]) {
    const live = sel.id === "harvest-spk";
    sel.textContent = "";
    for (const s of speakers.filter(s => s.consent && s.consent.scope_asr && (!live || s.consent.harvest_live)))
      sel.append(el("option", `${s.label}（${fmtMinutes(s.minutes_total)}）`, {value: s.id}));
    if (!sel.options.length) sel.append(el("option", live ? "（沒有允許現場收錄的講者）" : "（沒有已同意的講者）", {value: ""}));
  }
  return o;
}
let consentFor = null;
function openConsent(s) {
  consentFor = s; $("#consent-who").textContent = s.label; $("#consent-box").hidden = false;
  const f = $("#consent-form"); f.consent_date.value = new Date().toLocaleDateString("sv-SE", {timeZone: "Asia/Taipei"});
}
$("#consent-cancel").onclick = () => { $("#consent-box").hidden = true; };
$("#consent-form").onsubmit = async (ev) => {
  ev.preventDefault(); const f = ev.target;
  await api(`${T}/speakers/${consentFor.id}/consent`, {method: "POST", json: {
    scope_asr: f.scope_asr.checked, scope_mt: f.scope_mt.checked, harvest_live: f.harvest_live.checked,
    consent_date: f.consent_date.value, recorded_by: f.recorded_by.value, confirm_text: f.confirm_text.checked}});
  $("#consent-box").hidden = true; banner("已記錄同意", true); await loadOverview();
};
$("#spk-add").onsubmit = async (ev) => {
  ev.preventDefault(); const f = ev.target;
  const s = await api(`${T}/speakers`, {method: "POST", json: {label: f.label.value, accent_note: f.accent_note.value}});
  f.reset(); await loadOverview(); openConsent(s);
};

// ---------------------------------------------------------------- studio
const st = {rid: null, speaker: null, source: "", prompts: [], cur: null, lastClip: null, ctx: null, node: null,
  stream: null, chunks: [], recording: false, lastSaved: 0, idleTimer: null, log: []};

async function loadPrompts() {
  const r = await api(`${T}/prompts?speaker_id=${encodeURIComponent(st.speaker)}&n=20${st.source ? "&source=" + st.source : ""}`);
  st.prompts = r.prompts;
  const p = Object.entries(r.progress).map(([k, v]) => `${SOURCE_ZH[k] || k} ${v.done}/${v.total}`).join("　");
  const sp = await api(`${T}/speakers/${st.speaker}`);
  $("#studio-progress").textContent = `已錄 ${fmtMinutes(sp.minutes_total)}　｜　${p}`;
}
function showPrompt() {
  st.cur = st.prompts.shift() || null; st.lastClip = null; $("#redo-btn").disabled = true;
  $("#take-result").textContent = ""; $("#take-audio").hidden = true;
  $("#prompt").textContent = st.cur ? st.cur.text : "這個來源的提示句都錄完了，換一個來源或新增自訂句。";
  $("#prompt-src").textContent = st.cur ? (SOURCE_ZH[st.cur.source] || "") + (st.cur.term ? `：${st.cur.term}` : "") : "";
}
$("#studio-start").onsubmit = async (ev) => {
  ev.preventDefault(); const f = ev.target;
  if (!f.speaker_id.value) return banner("請先在「講者與同意」記錄同意");
  const rec = await api(`${T}/studio`, {method: "POST", json: {speaker_id: f.speaker_id.value}});
  Object.assign(st, {rid: rec.id, speaker: f.speaker_id.value, source: f.source.value, log: [], lastSaved: Date.now()});
  $("#take-log").textContent = ""; $("#studio").hidden = false; $("#studio-done").hidden = true;
  await loadPrompts(); showPrompt(); armIdle();
};
function armIdle() {
  clearInterval(st.idleTimer);
  st.idleTimer = setInterval(async () => {
    if (!st.rid || st.recording) return;
    const idle = Date.now() - st.lastSaved;
    if (idle >= IDLE_STOP_MS) { banner("5 分鐘沒有新錄音，已自動結束並存好逐字稿。", true); await finishStudio(); }
    else if (idle >= IDLE_WARN_MS) $("#meter-text").textContent = "⚠ 4 分鐘沒有新錄音；再 1 分鐘會自動結束並存檔。";
  }, 5000);
}
async function startRec() {
  if (!st.cur) return;
  st.stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false}});
  st.ctx = new AudioContext();
  await st.ctx.audioWorklet.addModule("/admin/training/rec-worklet.js");
  const src = st.ctx.createMediaStreamSource(st.stream);
  st.node = new AudioWorkletNode(st.ctx, "zen-recorder");
  st.chunks = []; st.recording = true;
  let clipSeen = false;
  st.node.port.onmessage = (e) => {
    st.chunks.push(e.data);
    const lv = level(e.data);
    const bar = $("#meter-bar"); bar.style.width = meterPct(lv.dbfs) + "%";
    if (lv.clipped) { clipSeen = true; bar.classList.add("hot"); }
    const secs = st.chunks.reduce((a, c) => a + c.length, 0) / st.ctx.sampleRate;
    $("#meter-text").textContent = `錄音中 ${secs.toFixed(1)} 秒` + (clipSeen ? "　⚠ 音量過大（削波），建議離麥克風遠一點" : "");
    if (secs > 30) stopRec();
  };
  src.connect(st.node);
  $("#rec-btn").classList.add("on"); $("#rec-btn").disabled = true; $("#stop-btn").disabled = false;
}
async function stopRec() {
  if (!st.recording) return;
  $("#stop-btn").disabled = true; $("#meter-text").textContent = "收尾音中…";
  await new Promise(r => setTimeout(r, TAIL_MS));
  st.recording = false;
  st.node.port.onmessage = null; st.node.disconnect();
  st.stream.getTracks().forEach(t => t.stop());
  const rate = st.ctx.sampleRate; await st.ctx.close();
  $("#rec-btn").classList.remove("on"); $("#rec-btn").disabled = false; $("#meter-bar").classList.remove("hot"); $("#meter-bar").style.width = "0";
  const n = st.chunks.reduce((a, c) => a + c.length, 0);
  const all = new Float32Array(n); let o = 0; for (const c of st.chunks) { all.set(c, o); o += c.length; }
  const pcm = resample(all, rate, TARGET_RATE);
  const warn = precheck(pcm);
  const q = new URLSearchParams({prompt_id: st.cur.id}); if (st.lastClip) q.set("replace", st.lastClip);
  const r = await api(`${T}/studio/${st.rid}/takes?${q}`, {method: "POST", body: encodeWav(pcm), headers: {"Content-Type": "audio/wav"}});
  const box = $("#take-result"); box.textContent = "";
  const probs = r.problems.length ? r.problems : warn;
  if (!r.saved) {
    box.append(el("p", "沒有存：" + probs.join("、") + "。請再錄一次。", {class: "tr-warn"}));
    $("#meter-text").textContent = "請重錄這句。"; return;
  }
  st.lastClip = r.clip.id; st.lastSaved = Date.now(); $("#redo-btn").disabled = false;
  const qd = r.quality;
  box.append(el("p", `已存 ${qd.duration_s.toFixed(1)} 秒｜音量 ${qd.rms_dbfs} dBFS｜SNR ${qd.snr_db} dB` + (probs.length ? "｜⚠ " + probs.join("、") + "（建議重錄）" : "｜品質良好"), {class: probs.length ? "tr-warn" : "tr-ok"}));
  const au = $("#take-audio"); au.src = `${T}/clips/${r.clip.id}/audio?v=${Date.now()}`; au.hidden = false;
  const li = el("li", `${st.cur.text}（${qd.duration_s.toFixed(1)} 秒）`, {"data-clip": r.clip.id});
  $("#take-log").querySelector(`[data-prompt="${st.cur.id}"]`)?.remove(); li.dataset.prompt = st.cur.id; $("#take-log").append(li);
  $("#meter-text").textContent = "聽一下，沒問題就按「下一句」。";
  await loadPrompts();
}
async function finishStudio() {
  if (!st.rid) return;
  clearInterval(st.idleTimer);
  const rec = await api(`${T}/studio/${st.rid}/finish`, {method: "POST", json: {}});
  const d = $("#studio-done"); d.textContent = ""; d.hidden = false; $("#studio").hidden = true;
  d.append(el("p", "這次錄音已結束，完整逐字稿已產生：", {class: "tr-ok"}), downloads(rec.id));
  st.rid = null; await loadOverview();
}
$("#rec-btn").onclick = () => startRec().catch(e => banner("無法開啟麥克風：" + e.message));
$("#stop-btn").onclick = () => stopRec().catch(() => {});
$("#redo-btn").onclick = () => { $("#take-result").textContent = "重錄：按「錄音」再讀一次，舊的那次會被取代。"; };
$("#next-btn").onclick = () => showPrompt();
$("#finish-btn").onclick = () => finishStudio().catch(() => {});
$("#prompt-add").onsubmit = async (ev) => { ev.preventDefault(); await api(`${T}/prompts`, {method: "POST", json: {text: ev.target.text.value}}); ev.target.reset(); await loadPrompts(); banner("已加入提示句", true); };

function downloads(rid, isClass = false) {
  const span = el("span");
  const files = [["transcript.txt", "TXT"], ["transcript.srt", "SRT"], ["transcript.vtt", "VTT"], ["transcript.json", "JSON"]];
  if (!isClass) files.push(["recording.wav", "音檔"]);
  for (const [name, label] of files) {
    span.append(el("a", label, {href: `${T}/recordings/${rid}/files/${name}`, download: `${rid}-${name}`}), " ");
  }
  span.append(el("a", isClass ? "全部＋整堂音檔（ZIP）" : "全部（ZIP）", {href: `${T}/recordings/${rid}/bundle.zip`, download: `${rid}.zip`}));
  return span;
}

// ---------------------------------------------------------------- class collection (primary)
const fmtDur = (sec) => { sec = Math.round(sec || 0); const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60); return h ? `${h} 小時 ${m} 分` : `${m} 分 ${sec % 60} 秒`; };
const STOP_ZH = {session_end: "下課（停止）", toggle_off: "主持人關閉", new_session: "換場次", no_audio: "5 分鐘沒有音訊", admin: "後台停止", recovered: "當機後恢復", withdrawn: "已撤回同意"};
try { const by = localStorage.getItem("zen-train-recorded-by"); if (by) { $("#quick-form").recorded_by.value = by; } } catch (_) { /* private mode */ }
$("#quick-form").onsubmit = async (ev) => {
  ev.preventDefault(); const f = ev.target;
  const s = await api(`${T}/speakers/quick`, {method: "POST", json: {label: f.label.value, recorded_by: f.recorded_by.value,
    scope_asr: f.scope_asr.checked, scope_mt: f.scope_mt.checked, harvest_live: f.harvest_live.checked, confirm_text: f.confirm_text.checked}});
  try { localStorage.setItem("zen-train-recorded-by", f.recorded_by.value); } catch (_) { /* ignore */ }
  f.label.value = ""; f.confirm_text.checked = false;
  banner(`已登記「${s.label}」並記錄同意。上課時在主持頁選他、打開「收錄訓練資料」即可。`, true);
  await loadOverview(); await loadHarvest();
};
async function loadHarvest() {
  const o = await loadOverview();
  const r = await api(`${T}/classes`);
  const sel = $("#harvest-spk"); sel.textContent = "";
  for (const s of r.lecturers) sel.append(el("option", s.label, {value: s.id}));
  if (!sel.options.length) sel.append(el("option", "（先快速登記主講者）", {value: ""}));
  const rt = $("#room-table tbody"); rt.textContent = "";
  const name = (id) => (speakers.find(s => s.id === id) || {}).label || "—";
  for (const x of o.rooms || []) {
    const tr = el("tr"); tr.append(el("td", x.room_id), el("td", name(x.speaker_id)), el("td", x.collect ? "開" : "關", {class: x.collect ? "tr-ok" : ""}), el("td", fmtTime(x.updated_at))); rt.append(tr);
  }
  const tb = $("#harvest-table tbody"); tb.textContent = "";
  for (const cs of r.classes) {
    const tr = el("tr");
    let state = cs.stopped_at ? `已結束：${STOP_ZH[cs.stop_reason] || cs.stop_reason || ""}` : "● 收錄中";
    if (cs.stopped_at && !cs.derived_at) state += "（等待課後整理）";
    if (cs.raw_deleted_at) state += "（原始音檔已依期限刪除）";
    const k = cs.clips;
    tr.append(el("td", fmtTime(cs.started_at)), el("td", cs.room_id), el("td", cs.speaker_label || "—"), el("td", fmtDur(cs.audio_s)),
      el("td", state, {class: cs.stopped_at ? "" : "tr-rec"}),
      el("td", `待審 ${k.to_review}・核准 ${k.approved}・排除 ${k.excluded}`));
    const td = el("td");
    if (!cs.stopped_at) td.append(btn("停止", async () => { await api(`${T}/classes/${cs.id}/stop`, {method: "POST", json: {}}); await loadHarvest(); }), " ");
    if (cs.recording_id) {
      td.append(btn("審核", async () => { openTab("classreview"); await loadClassList(cs.recording_id); }), " ", downloads(cs.recording_id, true));
    }
    tr.append(td); tb.append(tr);
  }
  if (!r.classes.length) { const tr = el("tr"); tr.append(el("td", "還沒有收錄紀錄。", {colspan: "7"})); tb.append(tr); }
}
$("#room-form").onsubmit = async (ev) => {
  ev.preventDefault(); const f = ev.target;
  await api(`${T}/classes/room`, {method: "POST", json: {room_id: f.room_id.value.trim(), speaker_id: f.speaker_id.value || null, collect: f.collect.checked}});
  lastOverview = null; banner("已儲存房間設定", true); await loadHarvest();
};
setInterval(() => { if (!$("#tab-harvest").hidden) loadHarvest().catch(() => {}); }, 10000);
function openTab(name) { const b = document.querySelector(`.tr-tabs button[data-tab="${name}"]`); for (const x of document.querySelectorAll(".tr-tabs button")) x.setAttribute("aria-selected", String(x === b)); for (const p of document.querySelectorAll(".tr-pane")) p.hidden = p.id !== "tab-" + name; }

// ---------------------------------------------------------------- post-class batch review
const cr = {rid: null, items: [], i: 0, editing: false};
async function loadClassList(pick) {
  const r = await api(`${T}/classes`);
  const sel = $("#cr-rid"); const prev = pick || sel.value; sel.textContent = "";
  for (const cs of r.classes.filter(x => x.recording_id)) sel.append(el("option", `${fmtTime(cs.started_at)}　${cs.speaker_label || ""}　待審 ${cs.clips.to_review}`, {value: cs.recording_id}));
  if (prev) sel.value = prev;
  const todo = r.classes.reduce((a, x) => a + x.clips.to_review, 0);
  $("#cq-count").textContent = todo ? `(${todo})` : "";
  if (sel.value) await loadBatch();
}
async function loadBatch() {
  const f = $("#cr-form");
  cr.rid = f.rid.value;
  if (!cr.rid) return;
  const q = new URLSearchParams({sort: f.sort.value, show: f.show.value, min_conf: f.min_conf.value});
  const b = await api(`${T}/classes/${cr.rid}/batch?${q}`);
  cr.items = b.items; cr.i = Math.min(cr.i, Math.max(0, b.items.length - 1));
  const n = b.counts, s = b.session || {};
  const note = s.derive_note ? `聲紋：${s.derive_note.backend === "sherpa" ? "CAM++ 模型" : "簡易（MFCC，建議安裝模型）"}` : (s.derived_at ? "" : "尚未課後整理（下課 1 分鐘後自動執行，或按「重新整理」）");
  $("#cr-summary").textContent = `共 ${n.total} 段：待審 ${n.todo}（其中 ${n.above_threshold} 段 ≥ 門檻，可一鍵核准）・已核准 ${n.approved}・已排除 ${n.excluded}。${note}`;
  $("#cr-approve-all").textContent = `核准門檻以上全部（${n.above_threshold} 段）`;
  renderBatch();
}
function renderBatch() {
  const ol = $("#cr-list"); ol.textContent = "";
  cr.items.forEach((it, k) => {
    const li = el("li", null, {class: "tr-row" + (k === cr.i ? " cur" : "") + (it.excluded ? " ex" : ""), "data-k": String(k)});
    const t = `${fmtClock(it.t0_ms)}`;
    const conf = it.excluded ? (it.excluded_reason === "question" ? "提問中" : "非主講者") : `信心 ${Math.round((it.confidence || 0) * 100)}`;
    const flags = (it.flags || []).map(f => FLAG_ZH[f] || f).join("、");
    li.append(el("span", t, {class: "tr-t"}), el("span", conf, {class: "tr-c " + ((it.confidence || 0) >= Number($("#cr-form").min_conf.value) && !it.excluded ? "tr-ok" : "tr-warn")}));
    const tx = el("span", it.text || it.asr_text || "", {class: "tr-x", lang: "zh-Hant"});
    li.append(tx, el("span", `${STATUS_ZH[it.status] || it.status}${flags ? "｜" + flags : ""}${it.spk_score != null ? "｜聲紋 " + it.spk_score.toFixed(2) : ""}`, {class: "meta"}));
    li.onclick = () => { cr.i = k; renderBatch(); };
    ol.append(li);
  });
  if (!cr.items.length) ol.append(el("li", "這裡沒有段落了。"));
  const cur = ol.querySelector(".cur"); if (cur) cur.scrollIntoView({block: "nearest"});
}
const fmtClock = (ms) => { const s = Math.floor((ms || 0) / 1000); return `${String(Math.floor(s / 3600)).padStart(2, "0")}:${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
function play() { const it = cr.items[cr.i]; if (!it || !it.has_audio) return banner("這段沒有音檔"); const a = $("#cr-audio"); a.src = `${T}/clips/${it.id}/audio`; a.play().catch(() => {}); }
function advance() { cr.items.splice(cr.i, 1); if (cr.i >= cr.items.length) cr.i = Math.max(0, cr.items.length - 1); renderBatch(); }
async function act(action) {
  const it = cr.items[cr.i]; if (!it) return;
  const show = $("#cr-form").show.value;
  if (action === "approve") { await api(`${T}/review/${it.id}`, {method: "POST", json: {action: "approve", stage_tm: false}}); advance(); }
  else if (action === "reject") { await api(`${T}/review/${it.id}`, {method: "POST", json: {action: "reject"}}); advance(); }
  else if (action === "exclude") { await api(`${T}/clips/${it.id}/exclude`, {method: "POST", json: {excluded: true}}); if (show === "todo") advance(); else { it.excluded = 1; renderBatch(); } }
  else if (action === "include") { await api(`${T}/clips/${it.id}/exclude`, {method: "POST", json: {excluded: false}}); if (show === "excluded") advance(); else { it.excluded = 0; renderBatch(); } }
}
function edit() {
  const it = cr.items[cr.i]; const li = $(`#cr-list li[data-k="${cr.i}"]`); if (!it || !li) return;
  const inp = el("input", null, {class: "tr-edit", "aria-label": "修改逐字稿"}); inp.value = it.text || it.asr_text || "";
  li.querySelector(".tr-x").replaceWith(inp); cr.editing = true; inp.focus();
  inp.onkeydown = async (ev) => {
    const a = reviewKey(ev.key, {editing: true}); if (!a) return; ev.preventDefault(); ev.stopPropagation();
    cr.editing = false;
    if (a === "save") { await api(`${T}/review/${it.id}`, {method: "POST", json: {action: "approve", text: inp.value, stage_tm: false}}); advance(); }
    else renderBatch();
  };
}
document.addEventListener("keydown", async (ev) => {
  if ($("#tab-classreview").hidden || cr.editing) return;
  if (ev.target.closest && ev.target.closest("input,select,textarea")) return;
  const a = reviewKey(ev.key, {ctrl: ev.ctrlKey || ev.metaKey || ev.altKey}); if (!a) return;
  ev.preventDefault();
  try {
    if (a === "next") { cr.i = Math.min(cr.items.length - 1, cr.i + 1); renderBatch(); }
    else if (a === "prev") { cr.i = Math.max(0, cr.i - 1); renderBatch(); }
    else if (a === "play") play();
    else if (a === "edit") edit();
    else await act(a);
  } catch (_) { /* banner shown */ }
});
$("#cr-form").onsubmit = (ev) => { ev.preventDefault(); cr.i = 0; loadBatch().catch(() => {}); };
$("#cr-rid").onchange = () => { cr.i = 0; loadBatch().catch(() => {}); };
$("#cr-approve-all").onclick = async () => {
  const m = Number($("#cr-form").min_conf.value);
  if (!confirm(`核准這堂課所有信心 ≥ ${m} 的主講者段落？（文字保持辨識結果）`)) return;
  const r = await api(`${T}/classes/${cr.rid}/approve-above`, {method: "POST", json: {min_conf: m}});
  banner(`已核准 ${r.approved} 段`, true); await loadBatch();
};
$("#cr-derive").onclick = async () => {
  banner("課後整理中…（一小時的課約需 1–2 分鐘）", true);
  const r = await api(`${T}/classes/${cr.rid}/derive`, {method: "POST", json: {}});
  banner(r.skipped ? `沒有整理：${r.skipped}` : `完成：${r.segments} 段，排除 ${r.excluded} 段`, true); await loadBatch();
};

// ---------------------------------------------------------------- hours per speaker
async function loadHours() {
  const r = await api(`${T}/stats/hours`);
  const box = $("#hours-box"); box.textContent = "";
  const entries = Object.entries(r.speakers);
  if (!entries.length) { box.append(el("p", "還沒有資料。")); return; }
  const max = Math.max(0.01, ...entries.flatMap(([, v]) => v.series.map(x => x.collected_h)));
  for (const [, v] of entries) {
    box.append(el("h4", `${v.label}：收錄 ${v.collected_total_h.toFixed(2)} 小時・核准 ${v.approved_total_h.toFixed(2)} 小時`));
    const t = el("table", null, {class: "tr-hours", "aria-label": `${v.label} 每週時數`}); const tb = el("tbody");
    for (const x of v.series) {
      const tr = el("tr"); const bar = el("td");
      const b1 = el("div", null, {class: "bar c", title: `收錄 ${x.collected_h} 小時`}); b1.style.width = (x.collected_h / max * 100) + "%";
      const b2 = el("div", null, {class: "bar a", title: `核准 ${x.approved_h} 小時`}); b2.style.width = (x.approved_h / max * 100) + "%";
      bar.append(b1, b2);
      tr.append(el("td", x.bucket), bar, el("td", `${x.collected_h.toFixed(2)} / ${x.approved_h.toFixed(2)} 小時`)); tb.append(tr);
    }
    t.append(tb); box.append(t);
  }
}

// ---------------------------------------------------------------- retention + disk
async function loadSettings() {
  const r = await api(`${T}/settings`);
  const f = $("#settings-form");
  for (const k of ["raw_retention_days", "excluded_audio_days", "auto_approve_min"]) f[k].value = r[k];
  $("#cr-form").min_conf.value = r.auto_approve_min;
  const d = r.disk, mb = (b) => (b / 1e6).toFixed(1) + " MB";
  $("#disk-note").textContent = d.estimate.note;
  const tb = $("#disk-table tbody"); tb.textContent = "";
  for (const [k, v] of [["整堂原始音檔", mb(d.raw_bytes)], ["切好的片段", mb(d.clip_bytes)], ["累積收錄", d.class_hours + " 小時"],
    ["估計：每小時課（原始）", d.estimate.raw_mb_per_class_hour + " MB"], ["估計：每小時課（片段）", d.estimate.clips_mb_per_class_hour + " MB"]]) {
    const tr = el("tr"); tr.append(el("th", k), el("td", v)); tb.append(tr);
  }
}
$("#settings-form").onsubmit = async (ev) => {
  ev.preventDefault(); const f = ev.target;
  await api(`${T}/settings`, {method: "POST", json: {raw_retention_days: Number(f.raw_retention_days.value), excluded_audio_days: Number(f.excluded_audio_days.value), auto_approve_min: Number(f.auto_approve_min.value)}});
  banner("已儲存", true); await loadSettings();
};
$("#cleanup-now").onclick = async () => { const r = await api(`${T}/cleanup`, {method: "POST", json: {}}); banner(`已釋放 ${(r.freed_bytes / 1e6).toFixed(1)} MB`, true); await loadSettings(); };

// ---------------------------------------------------------------- review
async function loadReview() {
  const f = $("#review-filter");
  const r = await api(`${T}/review?status=${f.status.value}${f.origin.value ? "&origin=" + f.origin.value : ""}`);
  const box = $("#review-list"); box.textContent = "";
  if (!r.items.length) box.append(el("p", "目前沒有項目。"));
  for (const it of r.items) box.append(reviewCard(it));
}
function reviewCard(it) {
  const card = el("div", null, {class: "tr-card"});
  const meta = `${STATUS_ZH[it.status] || it.status}｜${({harvest: "社課", studio: "錄音室", import: "匯入"})[it.origin]}｜${it.speaker_tag || "Speaker 0"}` +
    (it.speaker_label ? `→${it.speaker_label}` : "（未對應講者，已排除）") + (it.duration_s ? `｜${it.duration_s.toFixed(1)} 秒` : "") +
    (it.cer != null ? `｜Breeze 差異 ${(it.cer * 100).toFixed(0)}%` : "") + (it.align === "forced" ? "｜⚠ 時間為估計對齊" : "");
  card.append(el("div", meta, {class: "meta"}));
  if (it.audio_path) { const a = el("audio", null, {controls: "", preload: "none", src: `${T}/clips/${it.id}/audio`}); card.append(a); }
  if (it.asr_text) card.append(el("div", "Breeze：" + it.asr_text, {class: "meta"}));
  const ta = el("textarea", null, {"aria-label": "修正後逐字稿"}); ta.value = it.text || it.asr_text || "";
  const led = it.ledger || {};
  if (led.transcript && led.transcript.origin === "human") ta.value = led.transcript.text;
  card.append(el("label", "逐字稿（改成實際說的話）"), ta);
  const lang = el("select", null, {"aria-label": "譯文語言"}); for (const [v, l] of [["en", "英文"], ["ja", "日文"]]) lang.append(el("option", l, {value: v}));
  const tr = (led.translations || [])[0];
  lang.value = it.tgt_lang || (tr && tr.tgt_lang) || "en";
  const mt = el("textarea", null, {"aria-label": "修正後譯文"}); mt.value = it.mt_text || (tr && tr.text) || "";
  card.append(el("label", "譯文（可空；有填會成為翻譯訓練配對，並送到「待審修正」進翻譯記憶）"), lang, mt);
  const tag = el("input", null, {"aria-label": "講者標籤", maxlength: "24"}); tag.value = it.speaker_tag || "Speaker 0";
  const acts = el("div", null, {class: "tr-actions"});
  if (it.status !== "approved" && it.audio_path && it.speaker_id) acts.append(btn("核准", async () => { await decide(it, "approve", ta.value, mt.value, lang.value); card.remove(); }));
  if (it.status !== "rejected") acts.append(btn("退回（刪除音檔）", async () => { await decide(it, "reject"); card.remove(); }));
  acts.append(el("span", " 講者標籤 "), tag, btn("套用這段", async () => { await api(`${T}/clips/${it.id}/speaker`, {method: "POST", json: {tag: tag.value}}); banner("已更新講者標籤", true); }));
  card.append(acts);
  return card;
}
async function decide(it, action, text, mt_text, tgt_lang) {
  const r = await api(`${T}/review/${it.id}`, {method: "POST", json: {action, text, mt_text, tgt_lang}});
  banner(action === "approve" ? "已核准" + (r.staging && r.staging.staging_id ? `，譯文已送「待審修正」#${r.staging.staging_id}` : "") : "已退回並刪除音檔", true);
  loadOverview();
}
$("#review-filter").onsubmit = (ev) => { ev.preventDefault(); loadReview(); };

// ---------------------------------------------------------------- recordings
async function loadRecordings() {
  const r = await api(`${T}/recordings`);
  const tb = $("#rec-table tbody"); tb.textContent = "";
  for (const rec of r.recordings) {
    const tr = el("tr");
    tr.append(el("td", ({studio: "錄音室", harvest: "社課", import: "匯入"})[rec.kind] + (rec.status === "recording" ? "（進行中）" : "")),
      el("td", rec.title || rec.id), el("td", rec.clips), el("td", fmtMinutes(rec.seconds / 60)));
    const dl = el("td"); dl.append(downloads(rec.id)); tr.append(dl);
    const td = el("td"); td.append(btn("講者標籤…", () => tagDialog(rec))); tr.append(td);
    tb.append(tr);
  }
}
async function tagDialog(rec) {
  const tag = prompt("要處理哪個講者標籤？（例如 Speaker 1）", "Speaker 0"); if (!tag) return;
  const what = prompt(`「${tag}」：輸入新名稱改名；輸入講者 ID（spk_…）對應到名冊；輸入「排除」刪掉這位的音檔。\n名冊：` + speakers.filter(s => s.consent).map(s => `${s.label}=${s.id}`).join("，"));
  if (!what) return;
  const json = what === "排除" ? {tag, speaker_id: null} : what.startsWith("spk_") ? {tag, speaker_id: what} : {tag, rename_to: what};
  await api(`${T}/recordings/${rec.id}/speakers`, {method: "POST", json});
  banner("已套用到整份逐字稿", true); loadRecordings();
}

// ---------------------------------------------------------------- import
async function upload(file) {
  return api(`${T}/imports/upload?name=${encodeURIComponent(file.name)}`, {method: "POST", body: file, headers: {"Content-Type": "application/octet-stream"}});
}
$("#import-form").onsubmit = async (ev) => {
  ev.preventDefault(); const f = ev.target; const out = $("#import-result");
  out.textContent = "上傳中（只傳到這台電腦的後台）…";
  const a = f.audio.files[0], t = f.transcript.files[0];
  await upload(a); await upload(t);
  out.textContent = "轉檔與解析中…";
  const r = await api(`${T}/imports`, {method: "POST", json: {audio: a.name, transcript: t.name, title: f.title.value}});
  out.textContent = "";
  out.append(el("p", `已匯入「${r.title}」：所有講者預設排除，請把有同意的講者對應到名冊。`, {class: "tr-ok"}));
  for (const s of r.speakers) {
    const row = el("div", `${s.tag}：${s.segments} 段`);
    const sel = el("select"); sel.append(el("option", "（排除）", {value: ""}));
    for (const sp of speakers.filter(x => x.consent && x.consent.scope_asr)) sel.append(el("option", sp.label, {value: sp.id}));
    row.append(" → ", sel, btn("對應", async () => { if (!sel.value) return; const m = await api(`${T}/imports/${r.id}/map`, {method: "POST", json: {tag: s.tag, speaker_id: sel.value}}); banner(`已切出片段，進入審核佇列（${m.speakers.find(x => x.tag === s.tag).cut} 段）`, true); }));
    out.append(row);
  }
  out.append(btn("處理完了：刪除原始整段音檔", async () => { await api(`${T}/imports/${r.id}/purge-source`, {method: "POST", json: {}}); banner("已刪除原始整段音檔", true); }));
  f.reset();
};

// ---------------------------------------------------------------- export
async function loadExports() {
  const r = await api(`${T}/exports`);
  const tb = $("#export-table tbody"); tb.textContent = "";
  for (const e of r.exports) {
    const s = JSON.parse(e.stats || "{}"); const h = s.asr ? s.asr.hours : {};
    const tr = el("tr");
    tr.append(el("td", e.id), el("td", fmtTime(e.created_at)),
      el("td", e.deleted_at ? `已刪除（${e.deleted_reason === "speaker_withdrawn" ? "講者撤回" : "手動"}）` : `ASR ${((h.train || 0) + (h.validation || 0) + (h.test || 0)).toFixed(2)} 小時｜${e.dir}`));
    const td = el("td"); if (!e.deleted_at) td.append(btn("刪除", async () => { if (confirm("刪除這份匯出？")) { await api(`${T}/exports/${e.id}`, {method: "DELETE"}); loadExports(); } }));
    tr.append(td); tb.append(tr);
  }
}
$("#export-form").onsubmit = async (ev) => {
  ev.preventDefault(); const f = ev.target; const out = $("#export-result"); out.textContent = "建立中…";
  const r = await api(`${T}/exports`, {method: "POST", json: {options: {min_s: +f.min_s.value, max_s: +f.max_s.value, min_snr_db: +f.min_snr_db.value, max_cer: +f.max_cer.value, include_unverified: f.include_unverified.checked, include_tm: f.include_tm.checked}}});
  const h = r.asr.hours; out.textContent = "";
  out.append(el("p", `已建立 ${r.export_id}：train ${h.train.toFixed(3)} 小時、validation ${h.validation.toFixed(3)}、test ${h.test.toFixed(3)}；術語涵蓋 ${r.terms.covered}/${r.terms.total}。位置：${r.dir}`, {class: "tr-ok"}));
  loadExports();
};

// ---------------------------------------------------------------- boot
(async () => {
  try {
    const s = await api(API + "/auth/session"); csrf = s.csrf || null;
    $("#who").textContent = s.role ? `角色：${s.role}` : "";
  } catch (_) { return; }
  await loadHarvest();
  loadClassList().catch(() => {});
  loadSettings().catch(() => {});
})();
