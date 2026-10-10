// 示意圖 V2 viewer. Listens on the visual channel only (/ws/visual), never on the caption socket.
// Everything is rendered with textContent: model output is never parsed as HTML.
// ES module (static/*.js are modules; test_static_cache expects an export).
export const ROOM_RE = /^[A-Za-z0-9_-]{1,64}$/;
export const MAX_CARDS = 20;

export function pickRoom(search) {
  var params = new URLSearchParams(search || "");
  var room = params.get("room") || params.get("room_id") || "class";
  return ROOM_RE.test(room) ? room : "class";
}

export function visualSocketUrl(loc, room) {
  var proto = loc.protocol === "https:" ? "wss:" : "ws:";
  return proto + "//" + loc.host + "/ws/visual?room_id=" + encodeURIComponent(room);
}

export function start(doc, loc) {
  var document = doc;
  var location = loc;
  var params = new URLSearchParams(location.search);
  var room = pickRoom(location.search);
  var statusEl = document.getElementById("status");
  var list = document.getElementById("drafts");
  var empty = document.getElementById("empty");
  var input = document.getElementById("room-input");
  var seen = Object.create(null);
  var socket = null;
  var retry = 1000;

  input.value = room;
  document.getElementById("room-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var next = (input.value || "").trim();
    if (!ROOM_RE.test(next)) return;
    params.set("room", next);
    location.search = params.toString();
  });

  function setStatus(state, text) {
    statusEl.dataset.state = state;
    statusEl.textContent = text;
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  function render(draft) {
    if (!draft || draft.type !== "visual_draft" || draft.room_id !== room) return;
    if (seen[draft.id]) return;
    seen[draft.id] = true;
    var card = el("article", "card");
    card.dataset.kind = String(draft.kind || "");
    card.appendChild(el("h2", "", String(draft.title_zh || "示意圖")));
    if (draft.title_en) card.appendChild(el("p", "en", String(draft.title_en)));
    var secs = Math.round((Number(draft.t0_ms) || 0) / 1000);
    var meta = String(draft.kind) + " · v" + draft.version + " · " + Math.floor(secs / 60) + ":" +
      String(secs % 60).padStart(2, "0") + (draft.origin === "fallback" ? " · 術語卡（模型未回應）" : "");
    card.appendChild(el("div", "meta", meta));
    if (draft.body_zh) card.appendChild(el("div", "body", String(draft.body_zh)));
    if (draft.body_en) card.appendChild(el("div", "body en", String(draft.body_en)));
    if (draft.mermaid) card.appendChild(el("pre", "mermaid-src", String(draft.mermaid)));
    if (Array.isArray(draft.terms) && draft.terms.length) {
      var ul = el("ul", "terms");
      draft.terms.forEach(function (t) {
        ul.appendChild(el("li", t && t.locked ? "locked" : "", String(t.zh) + (t.en ? " · " + String(t.en) : "")));
      });
      card.appendChild(ul);
    }
    if (empty && empty.parentNode) empty.parentNode.removeChild(empty);
    list.insertBefore(card, list.firstChild);
    while (list.children.length > MAX_CARDS) list.removeChild(list.lastChild);
  }

  function connect() {
    setStatus("connecting", "連線中…（房間 " + room + "）");
    socket = new WebSocket(visualSocketUrl(location, room));
    socket.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "visual_hello") {
        retry = 1000;
        setStatus(msg.enabled ? "live" : "disabled",
          msg.enabled ? "即時 · 房間 " + room : "示意圖未啟用（未設定本機模型）· 房間 " + room);
        (msg.history || []).forEach(render);
      } else if (msg.type === "visual_draft") {
        render(msg);
      } else if (msg.type === "visual_unavailable") {
        setStatus("full", "觀看人數已滿，稍後重試");
      }
    };
    socket.onclose = function () {
      setStatus("idle", "連線中斷，" + Math.round(retry / 1000) + " 秒後重連");
      setTimeout(connect, retry);
      retry = Math.min(retry * 2, 30000);
    };
  }

  connect();
}

if (typeof document !== "undefined" && document.getElementById("drafts")) {
  start(document, window.location);
}
