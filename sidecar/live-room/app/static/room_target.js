// round4 #7 (T8-T10): the session's target language on the audience page.
// Everything is built with text nodes / createElement - never HTML strings (XSS test).
export const TARGET_FONTS = {
  ja: '"Yu Gothic UI","Meiryo","Hiragino Sans","Noto Sans JP",sans-serif',
};

export function targetLang(item, fallback = "en") {
  const v = item && typeof item.tgt_lang === "string" ? item.tgt_lang : "";
  return v === "ja" || v === "en" ? v : fallback;
}

export function modeLabels(lang) {
  if (lang === "ja") return { both: "中日", en: "只看日文", target: "日文" };
  return { both: "中英", en: "只看英文", target: "英文" };
}

// ruby: annotate only the first line of the session where a term appears (round2 decision).
// The owning line keeps its ruby on every repaint; later lines never get it.
export function createRubyMemory() {
  const owner = new Map();
  return {
    allow(sessionId, text, lineId) {
      const key = String(sessionId || "") + "\u0000" + text;
      if (!owner.has(key)) { owner.set(key, lineId); return true; }
      return owner.get(key) === lineId;
    },
    reset() { owner.clear(); },
  };
}

function appendWithRuby(doc, el, chunk, pending) {
  let rest = chunk;
  while (rest) {
    let hit = null;
    let at = -1;
    for (const r of pending) {
      const i = rest.indexOf(r.text);
      if (i >= 0 && (at < 0 || i < at)) { at = i; hit = r; }
    }
    if (!hit) { el.appendChild(doc.createTextNode(rest)); return; }
    if (at > 0) el.appendChild(doc.createTextNode(rest.slice(0, at)));
    const ruby = doc.createElement("ruby");
    ruby.appendChild(doc.createTextNode(hit.text));
    const rt = doc.createElement("rt");
    rt.appendChild(doc.createTextNode(hit.reading));
    ruby.appendChild(rt);
    el.appendChild(ruby);
    pending.splice(pending.indexOf(hit), 1);
    rest = rest.slice(at + hit.text.length);
  }
}

// Paint the target line. ja: segments joined by <wbr>; ruby on first occurrence when enabled.
// Returns the ruby terms that were drawn (so callers can tell).
export function paintTarget(doc, el, item, { memory, rubyOn = true } = {}) {
  while (el.firstChild) el.removeChild(el.firstChild);
  const text = item && typeof item.en === "string" ? item.en : "";
  const lang = targetLang(item);
  el.lang = lang;
  if (!text) return [];
  if (lang !== "ja") { el.appendChild(doc.createTextNode(text)); return []; }
  const segs = Array.isArray(item.segments) && item.segments.join("") === text ? item.segments : [text];
  const pending = [];
  if (rubyOn && Array.isArray(item.ruby)) {
    for (const r of item.ruby) {
      if (!r || typeof r.text !== "string" || typeof r.reading !== "string" || !r.text || !r.reading) continue;
      if (!text.includes(r.text)) continue;
      if (memory && !memory.allow(item.session_id, r.text, item.id)) continue;
      pending.push({ text: r.text, reading: r.reading });
    }
  }
  const drawn = pending.map((r) => r.text);
  segs.forEach((seg, i) => {
    if (i > 0) el.appendChild(doc.createElement("wbr"));
    appendWithRuby(doc, el, String(seg), pending);
  });
  return drawn.filter((t) => !pending.some((p) => p.text === t));
}
