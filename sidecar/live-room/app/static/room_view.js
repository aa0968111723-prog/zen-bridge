// Audience page decisions that do not need a socket: stale lines, gap copy, contrast.

export const CAPTION_IDLE_MS = 20000;
export const GAP_TOAST_MS = 6000;
export const HISTORY_DIVIDER = "— 這裡漏了一小段 —";

function channel(hex, index) {
  const value = parseInt(hex.slice(1 + index * 2, 3 + index * 2), 16) / 255;
  if (value <= 0.04045) return value / 12.92;
  return ((value + 0.055) / 1.055) ** 2.4;
}

function luminance(hex) {
  return 0.2126 * channel(hex, 0) + 0.7152 * channel(hex, 1) + 0.0722 * channel(hex, 2);
}

export function contrast(fg, bg) {
  const lighter = Math.max(luminance(fg), luminance(bg));
  const darker = Math.min(luminance(fg), luminance(bg));
  return (lighter + 0.05) / (darker + 0.05);
}

export function stageMode({ kind, ageMs, hasCaption }) {
  if (kind === "ended") return "ended";
  if (kind === "device_offline" || kind === "reconnecting" || kind === "unreachable" || kind === "link_invalid") {
    return "stale";
  }
  if (hasCaption && Number(ageMs) > CAPTION_IDLE_MS) return "idle";
  return "live";
}

export function stageNote(mode, hostLive) {
  if (mode === "idle") return "目前沒有新字幕";
  if (mode === "stale") return "連線中斷，這是最後收到的字幕";
  if (mode === "ended") return "這堂課已結束";
  if (mode === "empty") return hostLive ? "等待主持人開始說話" : "等待主持人開始";
  return "";
}

// Waiting and offline already have their own line. Don't repeat it on the stage.
export function stageNoteFor(kind, mode, hostLive) {
  if (kind === "waiting_room" || kind === "device_offline") return "";
  return stageNote(mode, hostLive);
}

export function gapCopy(reset) {
  return reset ? "已重新整理字幕" : "漏了一小段，已接回最新內容";
}

export const EXPIRY_NOTE = "較早的字幕已超過保存時間，已從畫面上拿掉";

// A reopen replays captions_expired for rows this screen never showed.
// The notice is only for a line that was actually up.
export function expiryNotice(ids, displayed) {
  const list = Array.isArray(ids) ? ids : [];
  const has = displayed && typeof displayed.has === "function" ? (id) => displayed.has(id) : () => false;
  for (const id of list) {
    if (id && has(id)) return EXPIRY_NOTE;
  }
  return "";
}

// follow, or the reader is already near the bottom: keep the latest line in view.
// Otherwise put the scroll offset back after a rebuild.
export function historyStick(scrollTop, scrollHeight, clientHeight, follow) {
  const distance = Number(scrollHeight) - Number(scrollTop) - Number(clientHeight);
  const atBottom = distance < 48;
  return { stick: !!(follow || atBottom), restore: Number(scrollTop) || 0 };
}
