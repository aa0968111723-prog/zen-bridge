// 主持頁「收錄訓練資料」開關（docs/finetune-data.md）。獨立模組：不改 host.html 的主程式，只多一個 <script>。
// 選主講者 → 打開開關 → 這個房間之後每堂課自動收錄（記住設定）。收錄中顯示紅色指示；Q = 學員提問中。
const $ = (s) => document.querySelector(s);
let token = "", state = null, busy = false;

function el(tag, text, attrs) {
  const e = document.createElement(tag);
  if (text != null) e.textContent = text;
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}
const room = () => (($("#room") || {}).value || "class").trim() || "class";

const box = el("section", null, {id: "zen-train", "aria-label": "訓練資料收錄"});
Object.assign(box.style, {position: "fixed", right: "12px", bottom: "12px", zIndex: 50, background: "#fffaf0",
  border: "2px solid #c9a978", borderRadius: "12px", padding: "8px 12px", maxWidth: "320px", fontSize: "14px",
  boxShadow: "0 2px 8px rgba(0,0,0,.15)"});
const title = el("strong", "訓練資料");
const badge = el("span", "", {role: "status", "aria-live": "polite"});
Object.assign(badge.style, {marginLeft: "8px", fontWeight: "700"});
const sel = el("select", null, {"aria-label": "主講者"});
const lab = el("label");
const chk = el("input", null, {type: "checkbox"});
lab.append(chk, " 收錄訓練資料");
const qbtn = el("button", "提問中（Q）", {type: "button", "aria-pressed": "false", class: "secondary"});
const note = el("div", "", {class: "muted"});
Object.assign(note.style, {fontSize: "12px", marginTop: "4px"});
const row1 = el("div"); row1.append(title, badge);
const row2 = el("div"); row2.append(el("span", "主講者 "), sel);
const row3 = el("div"); row3.append(lab, " ", qbtn);
for (const r of [row2, row3]) r.style.marginTop = "4px";
box.append(row1, row2, row3, note);
box.hidden = true;
document.body.append(box);

async function auth() {
  if (token) return token;
  const r = await fetch("/api/host-token", {cache: "no-store"});
  if (!r.ok) throw new Error("no host token");
  token = (await r.json()).token || "";
  return token;
}
async function call(path, opts = {}) {
  const t = await auth();
  const r = await fetch(path, {...opts, cache: "no-store", headers: {authorization: "Bearer " + t, "content-type": "application/json"}});
  if (r.status === 401) { token = ""; throw new Error("401"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
  return data;
}

function paint() {
  const s = state;
  if (!s) return;
  box.hidden = false;
  if (!s.available) {
    sel.hidden = true; lab.hidden = true; qbtn.hidden = true;
    note.textContent = s.message || "";
    note.append(" ", el("a", "到後台登記", {href: "http://127.0.0.1:8791/admin/training", target: "_blank", rel: "noopener"}));
    badge.textContent = "";
    return;
  }
  sel.hidden = false; lab.hidden = false;
  if (document.activeElement !== sel) {
    const cur = (s.room && s.room.speaker_id) || sel.value;
    sel.textContent = "";
    if (!s.lecturers.length) sel.append(el("option", "（沒有允許收錄的講者）", {value: ""}));
    for (const l of s.lecturers) sel.append(el("option", l.label, {value: l.id}));
    if (cur && s.lecturers.some((l) => l.id === cur)) sel.value = cur;
  }
  const on = !!(s.room && s.room.collect && s.room.speaker_id === sel.value);
  if (!busy) chk.checked = on;
  const st = s.status || {};
  qbtn.hidden = !st.collecting;
  qbtn.setAttribute("aria-pressed", String(!!st.question));
  qbtn.textContent = st.question ? "提問結束（Q）" : "提問中（Q）";
  if (st.collecting) {
    const min = Math.floor((st.audio_s || 0) / 60);
    badge.textContent = st.question ? "● 收錄中（提問，預設排除）" : "● 收錄中";
    badge.style.color = st.question ? "#a3420f" : "#c0392b";
    box.style.borderColor = "#c0392b";
    let t = `已存 ${min} 分鐘、${st.finals || 0} 句，${((st.bytes || 0) / 1e6).toFixed(0)} MB。`;
    if (st.paused) t += " 5 分鐘沒有辨識結果：已存逐字稿，暫停存音（說話後自動接續）。";
    else if (st.warn) t += ` ⚠ ${Math.floor(st.no_final_s / 60)} 分鐘沒有辨識結果（靜坐中？）`;
    if (st.dropped) t += ` 寫入來不及丟掉 ${st.dropped} 段（字幕不受影響）。`;
    note.textContent = t;
  } else if (on) {
    badge.textContent = "○ 已開啟"; badge.style.color = "#2f6b45"; box.style.borderColor = "#2f6b45";
    note.textContent = "按「開始聽」後自動開始收錄這堂課（整堂音檔＋逐字稿，只存在這台電腦）。";
  } else {
    badge.textContent = ""; box.style.borderColor = "#c9a978";
    note.textContent = "打開後，這個房間之後每堂課都會自動收錄。";
  }
}

async function refresh() {
  try { state = await call("/api/training/class?room_id=" + encodeURIComponent(room())); paint(); }
  catch (_) { /* host page not authorised yet / server restarting */ }
}
async function save(collect) {
  busy = true;
  try {
    await call("/api/training/class", {method: "POST", body: JSON.stringify({room_id: room(), speaker_id: sel.value || null, collect})});
  } catch (e) { chk.checked = !collect; note.textContent = "無法儲存：" + e.message; }
  finally { busy = false; }
  await refresh();
}
chk.onchange = () => save(chk.checked);
sel.onchange = () => {
  const l = state && state.lecturers.find((x) => x.id === sel.value);
  const roomOn = state && state.room && state.room.collect && state.room.speaker_id === sel.value;
  chk.checked = !!(roomOn || (l && l.default_on));          // 開過一次的講者預設開
  save(chk.checked);
};
async function toggleQuestion() {
  const st = (state && state.status) || {};
  if (!st.collecting) return;
  await call("/api/training/class/mark", {method: "POST", body: JSON.stringify({room_id: room(), kind: "question", on: !st.question, wall_ms: Date.now()})}).catch(() => {});
  st.question = !st.question; paint();
  setTimeout(refresh, 400);
}
qbtn.onclick = toggleQuestion;
document.addEventListener("keydown", (ev) => {
  if ((ev.key === "q" || ev.key === "Q") && !ev.ctrlKey && !ev.metaKey && !ev.altKey &&
      !(ev.target.closest && ev.target.closest("input,select,textarea,[contenteditable]"))) {
    ev.preventDefault(); toggleQuestion();
  }
});
const roomEl = $("#room");
if (roomEl) roomEl.addEventListener("change", refresh);
refresh();
setInterval(refresh, 5000);

export { refresh as refreshTrainingPanel };
