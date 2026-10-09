export function compareCaptions(a, b) {
  const ord = (Number(a.session_ord) || 0) - (Number(b.session_ord) || 0);
  if (ord) return ord;
  const seq = (Number(a.seq) || 0) - (Number(b.seq) || 0);
  if (seq) return seq;
  return (Number(a.cursor) || 0) - (Number(b.cursor) || 0);
}

export function orderedCaptions(items) {
  return [...items.values()].sort(compareCaptions);
}

export function liveTail(items, follow = true, pinned = null) {
  if (!follow && pinned && items.has(pinned)) return items.get(pinned);
  const list = orderedCaptions(items);
  return list.length ? list[list.length - 1] : null;
}

function noteCursor(current, value) {
  if (value == null || value === "") return current;
  const n = Number(value);
  if (!Number.isFinite(n)) return current;
  return Math.max(current, n);
}

export function mergeCaptionUpdate(prev, incoming) {
  if (!incoming || incoming.id == null || incoming.id === "") return prev ?? null;
  const version = Number(incoming.version) || 1;
  const next = { ...incoming, version };
  if (!prev) return next;
  if ((Number(prev.version) || 1) >= version) return prev;
  return next;
}

export function createCaptionView(limit = 80) {
  const items = new Map();
  const deleted = new Set();
  let epochFloor = 0;
  function apply(item) {
    if (!item || typeof item !== "object") return;
    if (item.type === "captions_cleared") {
      for (const id of items.keys()) deleted.add(id);
      items.clear();
      const epoch = Number(item.epoch);
      if (Number.isFinite(epoch)) epochFloor = Math.max(epochFloor, epoch);
      return;
    }
    const epoch = Number(item.epoch);
    if (Number.isFinite(epoch) && epochFloor && epoch < epochFloor) return;
    if (!item.id) return;
    if (item.type === "caption_deleted") {
      deleted.add(item.id);
      items.delete(item.id);
      return;
    }
    if (deleted.has(item.id)) return;
    if (item.seq == null) return;
    const prev = items.get(item.id);
    const merged = mergeCaptionUpdate(prev, item);
    if (!merged || merged === prev) return;
    items.set(item.id, merged);
    while (items.size > limit) items.delete(items.keys().next().value);
  }
  return {
    items,
    deleted,
    apply,
    replace(rows) {
      items.clear();
      for (const item of rows || []) apply(item);
    },
    reset() {
      items.clear();
      deleted.clear();
      epochFloor = 0;
    },
  };
}

export function connectRoom({ room, url, onState, onEvent, onGap, onDelete, onClear, onBackfill, onReset, openSocket, sleep, now, staleMs }) {
  const versions = new Map();
  let cursor = 0;
  let seenEpoch = null;
  let connectedCursor = 0;
  let wantReplay = false;
  let attempt = 0;
  let stopped = false;
  let socket = null;
  let timer = null;
  let cancelWait = null;
  let lastMessageAt = 0;
  let resolveDone = null;
  const done = new Promise((resolve) => { resolveDone = resolve; });
  const opener = openSocket || ((address) => new WebSocket(address));
  const clock = typeof now === "function" ? now : () => Date.now();
  const staleAfter = Number(staleMs) > 0 ? Number(staleMs) : 35000;

  function markMessage() {
    lastMessageAt = clock();
  }

  function remember(item) {
    if (!item || item.id == null || item.id === "") return false;
    cursor = noteCursor(cursor, item.cursor);
    const version = Number(item.version) || 1;
    const key = (item.session_id || "") + ":" + item.id;
    if ((versions.get(key) || 0) >= version) return false;
    versions.set(key, version);
    if (versions.size > 500) versions.delete(versions.keys().next().value);
    return true;
  }

  function address() {
    const base = url();
    const join = base.includes("?") ? "&" : "?";
    connectedCursor = cursor;
    let query = "cursor=" + encodeURIComponent(String(cursor));
    if (wantReplay) {
      query += "&replay=1";
      wantReplay = false;
    }
    return base + join + query;
  }

  function isCaption(data) {
    if (!data || data.id == null || data.id === "") return false;
    const kind = data.type;
    if (kind === "caption_deleted" || kind === "captions_cleared" || kind === "ping" || kind === "hello" || kind === "room_unavailable") {
      return false;
    }
    return kind == null || kind === "" || kind === "caption" || kind === "final";
  }

  function deliver(data) {
    if (!data || typeof data !== "object") return;
    cursor = noteCursor(cursor, data.cursor);
    if (data.type === "caption_deleted") {
      if (data.id) versions.delete((data.session_id || "") + ":" + data.id);
      if (onDelete) onDelete(data);
      return;
    }
    if (data.type === "captions_cleared") {
      versions.clear();
      if (data.epoch != null && Number.isFinite(Number(data.epoch))) seenEpoch = Number(data.epoch);
      if (onClear) onClear(data);
      return;
    }
    if (isCaption(data) && remember(data)) onEvent(data);
  }

  function waitMs(ms) {
    if (sleep) return sleep(ms);
    return new Promise((resolve) => {
      const finish = () => {
        if (timer) clearTimeout(timer);
        timer = null;
        cancelWait = null;
        resolve();
      };
      cancelWait = finish;
      timer = setTimeout(finish, ms);
    });
  }

  function detach(ws) {
    if (!ws) return;
    ws.onopen = null;
    ws.onmessage = null;
    ws.onerror = null;
    ws.onclose = null;
  }

  async function loop() {
    while (!stopped && attempt < 8) {
      onState(attempt === 0 ? "連線中…" : "恢復中…");
      let ws;
      try {
        ws = opener(address());
      } catch {
        onState("服務離線");
        attempt += 1;
        await waitMs(Math.min(8000, 400 * 2 ** attempt));
        continue;
      }
      socket = ws;
      await new Promise((resolve) => {
        const finish = () => {
          if (cancelWait === finish) cancelWait = null;
          resolve();
        };
        cancelWait = finish;
        ws.onopen = () => {
          markMessage();
          onState("已連上 " + room);
        };
        ws.onmessage = (ev) => {
          let data;
          try {
            data = JSON.parse(ev.data);
          } catch {
            return;
          }
          if (!data || typeof data !== "object") return;
          markMessage();
          if (data.type === "ping") {
            try { ws.send(JSON.stringify({ type: "pong" })); } catch { /* closed */ }
            return;
          }
          if (data.type === "room_unavailable") {
            onState(data.reason === "full" ? "房間已滿，稍後再連" : "房間已結束");
            // "ended" is the host closing the room. Stop. unknown_or_ended still
            // retries: that reason is also what a not-yet-opened room sends.
            if (data.reason === "ended") stopped = true;
            try { ws.close(); } catch { /* reconnect uses the attempt counter */ }
            return;
          }
          if (data.type === "hello") {
            attempt = 0;
            const latest = Number(data.latest_cursor);
            const epoch = data.epoch == null ? null : Number(data.epoch);
            const cursorBehind = Number.isFinite(latest) && latest < cursor;
            const epochChanged = epoch != null && seenEpoch != null && epoch !== seenEpoch;
            if (cursorBehind || epochChanged) {
              versions.clear();
              cursor = 0;
              if (epoch != null && Number.isFinite(epoch)) seenEpoch = epoch;
              if (onReset) onReset(data);
              // The hello we are about to drop may be the only copy of the backlog.
              // The next connection asks for the full caption state.
              wantReplay = true;
              if (connectedCursor > 0) {
                try { ws.close(); } catch { /* reconnect below */ }
                return;
              }
              wantReplay = false;
            } else if (epoch != null && Number.isFinite(epoch)) {
              seenEpoch = epoch;
            }
            cursor = noteCursor(cursor, data.latest_cursor);
            if (data.gap && onGap) onGap(data);
            if (Array.isArray(data.backfill) && onBackfill) onBackfill(data.backfill);
            else for (const item of data.backfill || []) deliver(item);
            for (const item of data.history || []) deliver(item);
            for (const item of data.events || []) deliver(item);
            return;
          }
          cursor = noteCursor(cursor, data.latest_cursor);
          deliver(data);
        };
        ws.onclose = () => finish();
        ws.onerror = () => { try { ws.close(); } catch { finish(); } };
      });
      detach(ws);
      if (socket === ws) socket = null;
      if (stopped) break;
      attempt += 1;
      onState("斷線，正在重連");
      await waitMs(Math.min(8000, 400 * 2 ** attempt));
    }
    if (!stopped) onState("服務離線。可按重新連線。");
    resolveDone();
  }

  loop();
  return {
    done,
    get cursor() { return cursor; },
    nudge() {
      if (stopped) return;
      attempt = 0;
      if (socket && lastMessageAt && clock() - lastMessageAt > staleAfter) {
        const ws = socket;
        try { ws.close(); } catch { /* onclose reconnects */ }
      }
      // Backoff uses the timer. A live socket that is still receiving stays up.
      if (timer && cancelWait) cancelWait();
    },
    stop() {
      stopped = true;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      const cancel = cancelWait;
      cancelWait = null;
      if (socket) {
        const ws = socket;
        socket = null;
        detach(ws);
        try { ws.close(); } catch { /* already closed */ }
      }
      if (cancel) cancel();
    },
  };
}
