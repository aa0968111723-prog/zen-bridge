// OBS browser-source overlay for zen-bridge live captions.
// Pure helpers are exported for node --test; start() only runs in a browser.
// Caption text is written with textContent only; no HTML-parsing DOM APIs.

export const MAX_LINES = 2;

export const FONT_STACKS = Object.freeze({
  en: '"Segoe UI", "Helvetica Neue", Arial, "Noto Sans", "Liberation Sans", sans-serif',
  ja: '"Noto Sans JP", "Noto Sans CJK JP", "Source Han Sans JP", "Yu Gothic UI", "Yu Gothic", "Meiryo UI", Meiryo, "Hiragino Sans", "Hiragino Kaku Gothic ProN", "MS PGothic", sans-serif',
  zh: '"Noto Sans TC", "Noto Sans CJK TC", "Source Han Sans TC", "Microsoft JhengHei UI", "Microsoft JhengHei", "PingFang TC", "Heiti TC", sans-serif',
});

export const DEFAULTS = Object.freeze({
  room: "class",
  k: "",
  lang: "en",      // target language of this session: en | ja
  show: "tgt",     // tgt (translation) | zh (Chinese source) | both
  lines: 2,        // captions kept on screen, 1..2
  size: 48,        // px, 12..160
  scale: 1,        // 0.25..4, multiplies size
  pos: "bottom",   // bottom | top | middle
  align: "center", // center | left | right
  color: "#ffffff",
  outline: "#000000",
  ow: 3,           // outline width px, 0..12
  shadow: 1,       // 0 | 1
  margin: 40,      // px from the anchored edge, 0..400
  maxw: 90,        // % of viewport width, 30..100
  hide: 0,         // s; clear the screen after this much silence, 0 = never, max 600
});

const ROOM_RE = /^[A-Za-z0-9_-]{1,64}$/;
const KEY_RE = /^[A-Za-z0-9_\-.~]{1,256}$/;
const HEX_RE = /^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

function pick(value, allowed, fallback) {
  const v = String(value ?? "").trim().toLowerCase();
  return allowed.includes(v) ? v : fallback;
}

export function clampNumber(raw, min, max, fallback, integer = false) {
  if (raw == null || String(raw).trim() === "") return fallback;
  const n = Number(raw);
  if (!Number.isFinite(n)) return fallback;
  const v = Math.min(max, Math.max(min, n));
  return integer ? Math.round(v) : v;
}

export function normalizeColor(raw, fallback) {
  if (raw == null) return fallback;
  const m = HEX_RE.exec(String(raw).trim());
  if (!m) return fallback;
  let hex = m[1].toLowerCase();
  if (hex.length === 3) hex = hex.split("").map((c) => c + c).join("");
  return "#" + hex;
}

// search: a URLSearchParams, a query string, or a plain object.
// pathRoom: room id from /overlay/{room_id}; it wins over ?room=.
export function parseParams(search, pathRoom = "") {
  let get;
  if (search && typeof search.get === "function") get = (k) => search.get(k);
  else if (typeof search === "string") {
    const p = new URLSearchParams(search);
    get = (k) => p.get(k);
  } else {
    const o = search || {};
    get = (k) => (Object.prototype.hasOwnProperty.call(o, k) ? o[k] : null);
  }
  const d = DEFAULTS;
  const roomRaw = String(pathRoom || get("room") || "").trim();
  const keyRaw = String(get("k") || "").trim();
  return {
    room: ROOM_RE.test(roomRaw) ? roomRaw : d.room,
    k: KEY_RE.test(keyRaw) ? keyRaw : "",
    lang: pick(get("lang"), ["en", "ja"], d.lang),
    show: pick(get("show"), ["tgt", "zh", "both"], d.show),
    lines: clampNumber(get("lines"), 1, MAX_LINES, d.lines, true),
    size: clampNumber(get("size"), 12, 160, d.size),
    scale: clampNumber(get("scale"), 0.25, 4, d.scale),
    pos: pick(get("pos"), ["bottom", "top", "middle"], d.pos),
    align: pick(get("align"), ["center", "left", "right"], d.align),
    color: normalizeColor(get("color"), d.color),
    outline: normalizeColor(get("outline"), d.outline),
    ow: clampNumber(get("ow"), 0, 12, d.ow),
    shadow: clampNumber(get("shadow"), 0, 1, d.shadow, true),
    margin: clampNumber(get("margin"), 0, 400, d.margin, true),
    maxw: clampNumber(get("maxw"), 30, 100, d.maxw, true),
    hide: clampNumber(get("hide"), 0, 600, d.hide),
  };
}

// Room id from location.pathname "/overlay/<room>".
export function roomFromPath(pathname) {
  const m = /^\/overlay\/([^/]+)\/?$/.exec(String(pathname || ""));
  if (!m) return "";
  let raw;
  try { raw = decodeURIComponent(m[1]); } catch { return ""; }
  return ROOM_RE.test(raw) ? raw : "";
}

// Font and BCP-47 tag for one line kind: "tgt" follows lang, "zh" is the source.
export function fontFor(kind, lang) {
  if (kind === "zh") return { lang: "zh-Hant", font: FONT_STACKS.zh };
  return lang === "ja" ? { lang: "ja", font: FONT_STACKS.ja } : { lang: "en", font: FONT_STACKS.en };
}

// Effective font size in px: size × scale, kept in 8..320.
export function fontPx(params) {
  return Math.min(320, Math.max(8, Math.round(params.size * params.scale)));
}

export function textShadow(params) {
  const w = params.ow;
  const c = params.outline;
  const parts = [];
  if (w > 0) {
    // 8-direction outline; cheap and works in OBS CEF without -webkit-text-stroke artifacts.
    for (const [x, y] of [[-1, -1], [0, -1], [1, -1], [-1, 0], [1, 0], [-1, 1], [0, 1], [1, 1]]) {
      parts.push(`${x * w}px ${y * w}px 0 ${c}`);
    }
  }
  if (params.shadow) parts.push(`0 ${Math.max(2, w)}px ${Math.max(4, w * 2)}px rgba(0,0,0,0.75)`);
  return parts.length ? parts.join(", ") : "none";
}

const FAILED = new Set(["missing", "error", "timeout", "cancelled"]);
const CONTROL = new Set(["caption_deleted", "captions_cleared", "captions_expired", "ping", "hello", "room", "room_unavailable"]);

export function isCaption(item) {
  if (!item || typeof item !== "object" || item.id == null || item.id === "") return false;
  if (CONTROL.has(item.type)) return false;
  return item.type == null || item.type === "" || item.type === "caption" || item.type === "final";
}

function order(a, b) {
  return ((Number(a.session_ord) || 0) - (Number(b.session_ord) || 0)) ||
    ((Number(a.seq) || 0) - (Number(b.seq) || 0)) ||
    ((Number(a.cursor) || 0) - (Number(b.cursor) || 0));
}

// The wire carries the translation in "en" for both target languages today;
// a future per-language field (e.g. "ja") is preferred when present.
export function lineText(item, show, lang) {
  if (!item) return { tgt: "", zh: "" };
  const zh = typeof item.zh === "string" ? item.zh.trim() : "";
  let tgt = "";
  if (!FAILED.has(item.status)) {
    const own = lang !== "en" && typeof item[lang] === "string" ? item[lang] : "";
    tgt = String(own || (typeof item.en === "string" ? item.en : "")).trim();
  }
  if (show === "zh") return { tgt: "", zh };
  if (show === "both") return { tgt, zh };
  return { tgt, zh: "" };
}

// Keeps a small window of captions keyed by id (newest version wins) and
// returns the latest `lines` that have something to show.
export function createLineBuffer({ lines = MAX_LINES, show = "tgt", lang = "en", keep = 16 } = {}) {
  const cap = Math.min(MAX_LINES, Math.max(1, Math.floor(Number(lines) || MAX_LINES)));
  const items = new Map();
  const deleted = new Set();
  function trim() {
    if (items.size <= keep) return;
    const sorted = [...items.values()].sort(order);
    for (const it of sorted.slice(0, items.size - keep)) items.delete(it.id);
  }
  function apply(item) {
    if (!item || typeof item !== "object") return false;
    if (item.type === "captions_cleared") {
      const had = items.size > 0;
      items.clear();
      return had;
    }
    if (item.type === "captions_expired") {
      const ids = Array.isArray(item.ids) ? item.ids.slice() : [];
      if (item.id) ids.push(item.id);
      let changed = false;
      for (const id of ids) changed = items.delete(id) || changed;
      return changed;
    }
    if (item.type === "caption_deleted") {
      if (!item.id) return false;
      deleted.add(item.id);
      return items.delete(item.id);
    }
    if (!isCaption(item) || deleted.has(item.id)) return false;
    const prev = items.get(item.id);
    const version = Number(item.version) || 1;
    if (prev && (Number(prev.version) || 1) >= version) return false;
    items.set(item.id, { ...item, version });
    trim();
    return true;
  }
  function visible() {
    const out = [];
    const sorted = [...items.values()].sort(order);
    for (let i = sorted.length - 1; i >= 0 && out.length < cap; i -= 1) {
      const t = lineText(sorted[i], show, lang);
      if (t.tgt || t.zh) out.unshift({ id: sorted[i].id, ...t });
    }
    return out;
  }
  return {
    apply,
    visible,
    replace(rows) {
      items.clear();
      for (const r of rows || []) apply(r);
    },
    clear() { items.clear(); },
    get size() { return items.size; },
    get limit() { return cap; },
  };
}

// Reconnect wait: [0.5, 1] × min(30 s, 1 s · 2^(n-1)). A server retry hint is a floor.
export function backoffMs(failures, unit = Math.random(), hintMs = 0) {
  const n = Math.max(1, Math.floor(Number(failures) || 1));
  const u = Math.min(1, Math.max(0, Number(unit) || 0));
  const base = Math.min(30000, 1000 * 2 ** Math.min(n - 1, 15));
  const wait = Math.round(base * (0.5 + 0.5 * u));
  const hint = Number(hintMs);
  return Number.isFinite(hint) && hint > 0 ? Math.max(wait, Math.min(hint, 60000)) : wait;
}

export function listenUrl(loc, params, cid) {
  const proto = loc.protocol === "https:" ? "wss" : "ws";
  let url = `${proto}://${loc.host}/ws/listen?room_id=${encodeURIComponent(params.room)}`;
  if (params.k) url += "&k=" + encodeURIComponent(params.k);
  if (cid) url += "&cid=" + encodeURIComponent(cid);
  return url;
}

function randomCid() {
  const bytes = new Uint8Array(12);
  if (globalThis.crypto && crypto.getRandomValues) crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  return "obs" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function applyLook(doc, root, params) {
  const s = root.style;
  s.setProperty("--zo-size", fontPx(params) + "px");
  s.setProperty("--zo-color", params.color);
  s.setProperty("--zo-shadow", textShadow(params));
  s.setProperty("--zo-margin", params.margin + "px");
  s.setProperty("--zo-maxw", params.maxw + "vw");
  s.setProperty("--zo-align", params.align);
  root.dataset.pos = params.pos;
  root.dataset.lang = params.lang;
  doc.documentElement.lang = fontFor("tgt", params.lang).lang;
}

export function render(doc, box, rows) {
  const frag = doc.createDocumentFragment();
  for (const row of rows) {
    const line = doc.createElement("div");
    line.className = "zo-line";
    for (const kind of ["zh", "tgt"]) {
      if (!row[kind]) continue;
      const f = fontFor(kind, box.dataset.lang || "en");
      const span = doc.createElement("div");
      span.className = "zo-" + kind;
      span.lang = f.lang;
      span.style.fontFamily = f.font;
      span.textContent = row[kind];
      line.appendChild(span);
    }
    frag.appendChild(line);
  }
  box.replaceChildren(frag);
}

export function start(win = globalThis.window) {
  const doc = win.document;
  const box = doc.getElementById("zen-overlay");
  if (!box) return null;
  const params = parseParams(win.location.search, roomFromPath(win.location.pathname));
  applyLook(doc, box, params);
  const buffer = createLineBuffer(params);
  const cid = randomCid();
  let failures = 0;
  let hint = 0;
  let stopRetry = false;
  let idleTimer = 0;
  let shown = "";

  const setState = (s) => { doc.body.dataset.state = s; };
  const paint = () => {
    const rows = buffer.visible();
    const sig = JSON.stringify(rows);
    if (sig === shown) return;
    shown = sig;
    render(doc, box, rows);
    if (params.hide > 0) {
      win.clearTimeout(idleTimer);
      idleTimer = win.setTimeout(() => { buffer.clear(); shown = ""; render(doc, box, []); }, params.hide * 1000);
    }
  };

  function connect() {
    setState(failures ? "reconnecting" : "connecting");
    let ws;
    try { ws = new win.WebSocket(listenUrl(win.location, params, cid)); } catch { ws = null; }
    if (!ws) return retry();
    ws.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }
      if (!data || typeof data !== "object") return;
      if (data.type === "ping") {
        try { ws.send(JSON.stringify({ type: "pong" })); } catch { /* closing */ }
        return;
      }
      if (data.type === "room_unavailable") {
        hint = Number(data.retry_after_ms) || (data.reason === "ended" ? 15000 : 0);
        setState(String(data.reason || "unavailable"));
        return;
      }
      if (data.type === "hello") {
        failures = 0;
        hint = 0;
        setState("live");
        buffer.replace([...(data.backfill || []), ...(data.history || []), ...(data.events || [])]);
        paint();
        return;
      }
      if (buffer.apply(data)) paint();
    };
    ws.onclose = (ev) => {
      if (ev && ev.code === 4401) { setState("link_invalid"); hint = 60000; }
      retry();
    };
    ws.onerror = () => { try { ws.close(); } catch { /* onclose follows */ } };
  }

  function retry() {
    if (stopRetry) return;
    failures += 1;
    win.setTimeout(connect, backoffMs(failures, Math.random(), hint));
  }

  connect();
  return { params, buffer, stop() { stopRetry = true; } };
}

if (typeof window !== "undefined" && typeof document !== "undefined" && document.getElementById("zen-overlay")) {
  start(window);
}
