// Pure helpers for the admin UI (no DOM). Tested by tests/admin_logic.test.mjs.
export function problemMessage(status, body) {
  const detail = body && typeof body.detail === "string" ? body.detail : "";
  if (status === 401) return "登入已過期，請重新登入（在終端機執行 start_backend.ps1 -OpenAdmin）。";
  if (status === 403) return body && body.code === "csrf" ? "安全檢查失敗（CSRF），請重新整理頁面。"
    : "權限不足：這個動作需要更高的角色。" + (detail ? `（${detail}）` : "");
  if (status === 404) return "找不到資料，可能已被刪除。";
  if (status === 409) return detail || "資料衝突，請重新載入後再試。";
  if (status === 412) return "資料已被別人修改，請重新載入。";
  if (status === 428) return "需要先載入最新版本（缺少 If-Match）。";
  if (status === 429) return "操作太頻繁，請稍後再試。";
  if (status === 503) return "服務暫時無法使用（直播服務可能未啟動）。";
  if (status >= 500) return "伺服器發生錯誤，已記錄。";
  return detail || `發生錯誤（${status}）`;
}

export function backoff(attempt, base = 1000, cap = 30000) {
  const n = Math.max(0, attempt | 0);
  return Math.min(cap, base * 2 ** Math.min(n, 15));
}

export function isStale(lastTs, nowTs, limitS = 10) {
  if (typeof lastTs !== "number") return true;
  return nowTs - lastTs > limitS;
}

export function rtfColor(rtf) {
  if (typeof rtf !== "number" || !Number.isFinite(rtf)) return "unknown";
  return rtf < 0.5 ? "green" : rtf < 0.9 ? "amber" : "red";
}

export function fmtMs(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "—";
  const t = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return (h ? `${h}:` : "") + `${String(m).padStart(h ? 2 : 1, "0")}:${String(s).padStart(2, "0")}`;
}

export function fmtBytes(n) {
  if (typeof n !== "number") return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

// Workbench keyboard: j/k or arrows move, Enter edits, Escape cancels, Ctrl+Enter saves, Ctrl+Z undoes.
export function workbenchKey(ev, index, count) {
  const k = ev.key;
  if ((ev.ctrlKey || ev.metaKey) && k === "Enter") return {action: "save", index};
  if ((ev.ctrlKey || ev.metaKey) && (k === "z" || k === "Z")) return {action: "undo", index};
  if (k === "Escape") return {action: "cancel", index};
  if (ev.editing) return {action: "none", index};
  if (k === "j" || k === "ArrowDown") return {action: "move", index: Math.min(count - 1, index + 1)};
  if (k === "k" || k === "ArrowUp") return {action: "move", index: Math.max(0, index - 1)};
  if (k === "Enter" || k === "e") return {action: "edit", index};
  if (k === "t") return {action: "toggle-target", index};
  if (k === "p") return {action: "push", index};
  return {action: "none", index};
}

export function parseSse(chunk) {
  const out = [];
  for (const block of chunk.split("\n\n")) {
    let event = "message", data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7);
      else if (line.startsWith("data: ")) data += line.slice(6);
    }
    if (data) out.push({event, data});
  }
  return out;
}

export const PAGES = ["overview", "monitor", "sessions", "review", "glossary", "tm", "search", "exports", "config",
  "audit", "users"];
export function pageFromHash(hash) {
  const p = String(hash || "").replace(/^#\/?/, "").split("/")[0];
  return PAGES.includes(p) ? p : "overview";
}
