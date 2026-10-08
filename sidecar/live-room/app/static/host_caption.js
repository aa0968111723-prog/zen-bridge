// Host big-caption display. The page keeps the caption map in room_client's view;
// this module decides which line is on screen and refuses an older version.

function rank(item) {
  return [Number(item.session_ord) || 0, Number(item.seq) || 0, Number(item.cursor) || 0];
}

function later(a, b) {
  const left = rank(a);
  const right = rank(b);
  for (let i = 0; i < left.length; i += 1) {
    if (left[i] !== right[i]) return left[i] > right[i];
  }
  return false;
}

export function needsEnglishRetry(item) {
  if (!item) return false;
  if (typeof item.en === "string" && item.en) return false;
  return item.translate_status === "skipped_backlog" || item.status === "translate_failed";
}

export function englishLabel(item) {
  if (!item) return "";
  const en = typeof item.en === "string" ? item.en : "";
  if (en) return en;
  if (item.translate_status === "skipped_backlog") return "（英譯積壓已略過，可重試）";
  if (item.status === "translate_failed") return "（英譯失敗，可重試）";
  return "（尚無英譯）";
}

export function applyCaption(state, msg) {
  if (!state.items) state.items = new Map();
  if (msg && msg.id != null && msg.id !== "") {
    const version = Number(msg.version) || 1;
    const prev = state.items.get(msg.id);
    if (!prev || (Number(prev.version) || 1) < version) {
      state.items.set(msg.id, { ...msg, version });
    }
  }
  let live = null;
  for (const item of state.items.values()) {
    if (!live || later(item, live)) live = item;
  }
  if (!live) return { zh: "", en: "", label: "", item: null, retry: false };
  const zh = live.zh || live.error || "";
  const en = typeof live.en === "string" ? live.en : "";
  return { zh, en, label: englishLabel(live), item: live, retry: needsEnglishRetry(live) };
}
