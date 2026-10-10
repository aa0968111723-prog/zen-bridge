"use strict";
// Vanilla JS. Session = HttpOnly cookie; the CSRF token is kept in memory only.
const API = "/admin/api/v1";
let csrf = null;

async function api(path, opts = {}) {
  const headers = Object.assign({"Accept": "application/json"}, opts.headers || {});
  if (opts.method && opts.method !== "GET") {
    headers["X-Zen-CSRF"] = csrf;
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  }
  const r = await fetch(API + path, Object.assign({}, opts, {headers, credentials: "same-origin"}));
  if (r.status === 401) { document.body.innerHTML = "<p>登入已過期。請在終端機執行 start_backend.ps1 -OpenAdmin 重新登入。</p>"; throw new Error("401"); }
  return r;
}
const el = (tag, text) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; return e; };

async function loadSessions() {
  const body = document.querySelector("#sessions tbody"); body.textContent = "";
  const data = await (await api("/sessions?limit=20")).json();
  for (const s of data.items) {
    const tr = el("tr");
    [new Date(s.started_at * 1000).toLocaleString(), s.room_id, s.status, s.segments].forEach(v => tr.append(el("td", String(v))));
    body.append(tr);
  }
}

async function loadProposals() {
  const body = document.querySelector("#proposals tbody"); body.textContent = "";
  const data = await (await api("/glossary/proposals")).json();
  for (const t of data.items) {
    const tr = el("tr"); [t.zh, t.en, t.hit_count].forEach(v => tr.append(el("td", String(v))));
    const td = el("td");
    for (const [label, action] of [["通過", "approve"], ["駁回", "reject"]]) {
      const b = el("button", label);
      b.onclick = async () => {
        const r = await api(`/glossary/proposals/${t.id}/${action}`, {method: "POST", headers: {"If-Match": t.etag}});
        if (r.status === 412) alert("這個詞已被別人修改，重新載入。");
        loadProposals();
      };
      td.append(b);
    }
    tr.append(td); body.append(tr);
  }
}

document.getElementById("search").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const qs = new URLSearchParams({q: f.get("q"), scope: f.get("scope")});
  const data = await (await api("/segments/search?" + qs)).json();
  const ol = document.getElementById("hits"); ol.textContent = "";
  for (const h of (data.items || [])) ol.append(el("li", `${h.session_id} @${Math.round(h.t0_ms / 1000)}s  ${h.snippet}`));
  if (!ol.children.length) ol.append(el("li", "沒有結果"));
};

document.getElementById("logout").onclick = async () => {
  await api("/auth/logout", {method: "POST"}); location.replace("/admin/login");
};

(async () => {
  const me = await (await api("/auth/session")).json();
  csrf = me.csrf;
  document.getElementById("who").textContent = me.role;
  document.getElementById("health").textContent = JSON.stringify(await (await api("/health")).json(), null, 1);
  loadSessions(); loadProposals();
  // SSE: same-origin cookie auth; nothing secret in the URL.
  const es = new EventSource(API + "/live/stream");
  es.addEventListener("metrics", e => { document.getElementById("live").textContent = JSON.stringify(JSON.parse(e.data), null, 1); });
  es.addEventListener("down", () => { document.getElementById("live").textContent = "直播服務未啟動"; });
})();
