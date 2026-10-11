"use strict";
// The one-time code lives in the #fragment, which the browser never sends to the server
// or writes into server logs. It is removed from the address bar right away.
(async () => {
  const msg = document.getElementById("msg");
  const m = /[#&]code=([^&]+)/.exec(location.hash);
  history.replaceState(null, "", "/admin/login");
  if (!m) { msg.textContent = "缺少登入碼。請在終端機執行 start_backend.ps1 -OpenAdmin 取得新的登入網址。"; return; }
  const r = await fetch("/admin/api/v1/auth/login", {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({code: decodeURIComponent(m[1])}),
  });
  if (r.ok) { location.replace("/admin"); return; }
  const p = await r.json().catch(() => ({}));
  msg.textContent = "登入失敗：" + (p.detail || r.status);
})();
