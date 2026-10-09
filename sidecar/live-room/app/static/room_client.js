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
    if (item.type === "captions_expired") {
      const ids = Array.isArray(item.ids) ? item.ids : [];
      for (const id of ids) items.delete(id);
      if (item.id) items.delete(item.id);
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

export function audienceClientId(storage) {
  const key = "breeze.audience.cid";
  const valid = (value) => typeof value === "string" && /^[A-Za-z0-9_-]{8,64}$/.test(value);
  try {
    const existing = storage && storage.getItem(key);
    if (valid(existing)) return existing;
    const bytes = new Uint8Array(16);
    if (globalThis.crypto && typeof crypto.getRandomValues === "function") crypto.getRandomValues(bytes);
    else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
    const id = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    if (storage) storage.setItem(key, id);
    return id;
  } catch {
    return "tab" + Math.floor(Math.random() * 1e9).toString(16);
  }
}

function unitSpan(unit) {
  const rolled = Number(unit);
  return Number.isFinite(rolled) ? Math.min(1, Math.max(0, rolled)) : 0;
}

// Wait at least retry_after, then add up to half of that as jitter so a classroom
// does not retry on the same millisecond. unit is Math.random() in [0, 1].
export function deferredRetryMs(retryAfterMs, unit) {
  const span = unitSpan(unit);
  const hinted = Number(retryAfterMs);
  const base = Number.isFinite(hinted) && hinted > 0 ? hinted : 500;
  const jitterBase = Number.isFinite(hinted) && hinted > 0 ? hinted : 1000;
  return base + Math.round(jitterBase * 0.5 * span);
}

// After a live hello, reconnect waits [0.5, 1] × min(cap, 1s, 2s, 4s, …).
// The sixth failure and after is 30–60s (60s while the tab is hidden), not another doubling.
export function reconnectDelayMs(failures, unit, hidden) {
  const span = unitSpan(unit);
  const n = Math.max(1, Math.floor(Number(failures) || 1));
  if (n >= 6) return hidden ? 60000 : 30000 + Math.round(30000 * span);
  const cap = hidden ? 60000 : 30000;
  const base = Math.min(cap, 1000 * 2 ** (n - 1));
  return Math.round(base * (0.5 + 0.5 * span));
}

// Room not open yet: 5s ±30% for the first two minutes, then 15s ±30%. Never stops.
// A hidden tab waits 30–60s so a locked phone is not opening a socket every few seconds.
export function waitingDelayMs(elapsedMs, unit, hidden) {
  const span = unitSpan(unit);
  if (hidden) return 30000 + Math.round(30000 * span);
  const elapsed = Number(elapsedMs) || 0;
  const base = elapsed < 120000 ? 5000 : 15000;
  return Math.round(base * (0.7 + 0.6 * span));
}

// Room full: 20s ±30%.
export function fullDelayMs(unit) {
  return Math.round(20000 * (0.7 + 0.6 * unitSpan(unit)));
}

function stateText(kind, attempt) {
  switch (kind) {
    case "connecting": return "連線中";
    case "waiting_room": return "等待主持人";
    case "live": return "即時字幕";
    case "reconnecting": return "重連第 " + attempt + " 次";
    case "device_offline": return "手機沒網路";
    case "unreachable": return "暫時連不上";
    case "room_full": return "已滿";
    case "ended": return "已結束";
    case "link_invalid": return "連結已失效，請重新掃描";
    case "rejected": return "無法開啟";
    default: return "連線中";
  }
}

export function retryCountdown(nextRetryAt, now) {
  const at = Number(nextRetryAt) || 0;
  const current = Number(now) || 0;
  if (at > current) {
    const sec = Math.max(1, Math.ceil((at - current) / 1000));
    return "下次自動重試：" + sec + " 秒";
  }
  return "下次自動重試";
}

function retrySubtitle(more, now) {
  // The attempt has already started. "Next retry" is only for the wait before it.
  if (!more || !more.nextRetryAt) return "正在重試…";
  return retryCountdown(more.nextRetryAt, now);
}

function stateSubtitle(kind, more, now) {
  switch (kind) {
    case "waiting_room": return "房間還沒開始，開始後會自動顯示字幕";
    case "device_offline": return "";
    case "unreachable": return retrySubtitle(more, now);
    case "room_full": return "稍後自動再試";
    case "ended": return "最近字幕仍可往回看";
    case "link_invalid": return "請重新掃描主持人畫面上的 QR 碼";
    case "rejected": return "無法開啟這個房間，請確認網址或重新掃描 QR 碼";
    default: return "";
  }
}

function stateManual(kind, attempt) {
  if (kind === "reconnecting") return attempt >= 3;
  return kind === "unreachable" || kind === "room_full" || kind === "ended" || kind === "link_invalid" || kind === "rejected";
}

export function connectRoom({
  room, url, onState, onEvent, onGap, onDelete, onClear, onBackfill, onReset, onHost, onExpire,
  openSocket, sleep, now, staleMs, random, schedule, cancelSchedule, isForeground, network, watchEvery,
  extraHelloMs,
}) {
  const versions = new Map();
  let cursor = 0;
  let seenEpoch = null;
  // Set when this connection's captions must be replaced, but the replacement
  // has not arrived. The screen stays up until then.
  let replaceOnBackfill = false;
  let pendingWait = 0;
  let deferredThisAttempt = false;
  let failures = 0;
  let sawHello = false;
  let waitingSince = 0;
  let hold = null;
  let closeReason = "";
  let closeCode = 0;
  let skipAccounting = false;
  let stopped = false;
  let loopRunning = false;
  let socket = null;
  let extraSocket = null;
  let extraHelloTimer = null;
  // A supplement that is accepted and then never sent hello must not pin the
  // backfill flag. Server pings would otherwise keep that socket up forever.
  const extraHelloLimit = Number(extraHelloMs) > 0 ? Number(extraHelloMs) : 15000;
  let backfillGen = 0;
  let pullSerial = 0;
  let backfillTimer = null;
  let timer = null;
  let cancelWait = null;
  let waitFinish = null;
  let releaseHold = null;
  let releaseOnline = null;
  let lastMessageAt = 0;
  let watchId = null;
  let finished = false;
  let resolveDone = null;
  const done = new Promise((resolve) => { resolveDone = resolve; });
  const opener = openSocket || ((address) => new WebSocket(address));
  const clock = typeof now === "function" ? now : () => Date.now();
  const roll = typeof random === "function" ? random : () => Math.random();
  const staleAfter = Number(staleMs) > 0 ? Number(staleMs) : 35000;
  const watchMs = Number(watchEvery) > 0 ? Number(watchEvery) : 5000;
  const net = network || (typeof window !== "undefined" ? window : null);
  let online = !(net && typeof net.onLine === "boolean") || net.onLine !== false;
  const scheduleFn = typeof schedule === "function" ? schedule : (fn, ms) => {
    const id = setInterval(fn, ms);
    if (id && typeof id.unref === "function") id.unref();
    return id;
  };
  const cancelScheduleFn = typeof cancelSchedule === "function" ? cancelSchedule : (id) => clearInterval(id);

  function pageHidden() {
    if (typeof isForeground === "function") return !isForeground();
    if (typeof document !== "undefined" && document.visibilityState === "hidden") return true;
    return false;
  }

  function emit(kind, extra) {
    const more = extra || {};
    const attempt = more.attempt != null ? more.attempt : failures;
    const detail = {
      kind,
      text: more.text != null ? more.text : stateText(kind, attempt),
      subtitle: more.subtitle != null ? more.subtitle : stateSubtitle(kind, more, clock()),
      attempt,
      nextRetryAt: more.nextRetryAt || 0,
      manual: more.manual != null ? more.manual : stateManual(kind, attempt),
    };
    if (onState) onState(detail);
    return detail;
  }

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
    return listenAddress(false);
  }

  function listenAddress(supplement) {
    const base = url();
    const join = base.includes("?") ? "&" : "?";
    // Ask for the saved captions, but do not drop the cursor we already have
    // until that payload is applied. Only the background socket is a supplement:
    // the live reconnect still takes a listener seat.
    let query = replaceOnBackfill
      ? "cursor=0&replay=1"
      : "cursor=" + encodeURIComponent(String(cursor));
    if (supplement && replaceOnBackfill) query += "&supplement=1";
    return base + join + query;
  }

  function isCaption(data) {
    if (!data || data.id == null || data.id === "") return false;
    const kind = data.type;
    if (
      kind === "caption_deleted" || kind === "captions_cleared" || kind === "captions_expired" ||
      kind === "ping" || kind === "hello" || kind === "room_unavailable" || kind === "room"
    ) {
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
    if (data.type === "captions_expired") {
      const ids = Array.isArray(data.ids) ? data.ids.slice() : [];
      if (data.id && !ids.includes(data.id)) ids.push(data.id);
      for (const id of ids) {
        for (const key of [...versions.keys()]) {
          if (key === id || key.endsWith(":" + id)) versions.delete(key);
        }
      }
      if (onExpire) onExpire(data);
      if (onDelete) {
        for (const id of ids) onDelete({ type: "captions_expired", id, ids, room_id: data.room_id });
      }
      return;
    }
    if (isCaption(data) && remember(data)) onEvent(data);
  }

  function clearBackfillTimer() {
    if (backfillTimer == null) return;
    clearTimeout(backfillTimer);
    backfillTimer = null;
  }

  function clearExtraHelloTimer() {
    if (extraHelloTimer == null) return;
    clearTimeout(extraHelloTimer);
    extraHelloTimer = null;
  }

  function armExtraHello(extra, gen) {
    clearExtraHelloTimer();
    const timer = setTimeout(() => {
      if (extraHelloTimer === timer) extraHelloTimer = null;
      if (stopped || gen !== backfillGen || extraSocket !== extra) return;
      try { extra.close(); } catch { /* onclose schedules the next pull */ }
    }, extraHelloLimit);
    if (timer && typeof timer.unref === "function") timer.unref();
    extraHelloTimer = timer;
  }

  function closeExtra() {
    // The background replay socket holds a server connection until the heartbeat
    // times out unless we close it as soon as the replacement is applied.
    clearBackfillTimer();
    clearExtraHelloTimer();
    if (!extraSocket) return;
    const extra = extraSocket;
    extraSocket = null;
    extra.onclose = null;
    try { extra.close(); } catch { /* already closed */ }
  }

  function acceptReplace(data) {
    const epoch = data.epoch == null ? null : Number(data.epoch);
    const hasBackfill = Array.isArray(data.backfill);
    versions.clear();
    cursor = 0;
    if (epoch != null && Number.isFinite(epoch)) seenEpoch = epoch;
    replaceOnBackfill = false;
    backfillGen += 1;
    closeExtra();
    if (onReset) onReset(data);
    if (hasBackfill && onBackfill) onBackfill(data.backfill);
    else for (const item of data.backfill || []) deliver(item);
    cursor = noteCursor(cursor, data.latest_cursor);
    emit("live");
    if (data.gap && onGap) onGap(data);
    for (const item of data.history || []) deliver(item);
    for (const item of data.events || []) deliver(item);
  }

  function wireExtra(extra, gen) {
    extraSocket = extra;
    armExtraHello(extra, gen);
    extra.onopen = () => {};
    extra.onerror = () => { try { extra.close(); } catch { /* onclose retries */ } };
    extra.onclose = () => {
      clearExtraHelloTimer();
      if (extraSocket === extra) extraSocket = null;
      if (stopped || gen !== backfillGen || !replaceOnBackfill) return;
      scheduleBackfill(deferredRetryMs(1000, roll()));
    };
    extra.onmessage = (ev) => {
      if (stopped || gen !== backfillGen) return;
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (!msg || typeof msg !== "object") return;
      if (msg.type === "ping") {
        try { extra.send(JSON.stringify({ type: "pong" })); } catch { /* closed */ }
        return;
      }
      if (msg.type !== "hello") {
        cursor = noteCursor(cursor, msg.latest_cursor);
        deliver(msg);
        return;
      }
      clearExtraHelloTimer();
      if (msg.backfill_deferred === true) {
        const hinted = Number(msg.retry_after_ms != null ? msg.retry_after_ms : msg.retry_after);
        const again = deferredRetryMs(Number.isFinite(hinted) ? hinted : 1000, roll());
        extra.onclose = null;
        if (extraSocket === extra) extraSocket = null;
        try { extra.close(); } catch { /* already closed */ }
        scheduleBackfill(again);
        return;
      }
      acceptReplace(msg);
      queueMicrotask(() => {
        if (extraSocket === extra) extraSocket = null;
        extra.onclose = null;
        try { extra.close(); } catch { /* already closed */ }
      });
    };
  }

  function scheduleBackfill(wait) {
    const gen = backfillGen;
    const serial = ++pullSerial;
    const pull = async () => {
      if (sleep) {
        try { await sleep(wait); } catch { return; }
      } else {
        await new Promise((resolve) => {
          const id = setTimeout(() => {
            if (backfillTimer === id) backfillTimer = null;
            resolve();
          }, wait);
          backfillTimer = id;
          if (id && typeof id.unref === "function") id.unref();
        });
      }
      if (stopped || gen !== backfillGen || serial !== pullSerial || !replaceOnBackfill) return;
      let extra = null;
      try { extra = opener(listenAddress(true)); } catch { extra = null; }
      if (!extra) {
        if (!stopped && gen === backfillGen && serial === pullSerial && replaceOnBackfill) {
          scheduleBackfill(deferredRetryMs(1000, roll()));
        }
        return;
      }
      wireExtra(extra, gen);
    };
    pull();
  }

  function waitMs(ms) {
    return new Promise((resolve) => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        timer = null;
        if (waitFinish === finish) waitFinish = null;
        if (cancelWait === finish) cancelWait = null;
        resolve();
      };
      waitFinish = finish;
      cancelWait = finish;
      if (sleep) {
        Promise.resolve().then(() => sleep(ms)).then(finish, finish);
        return;
      }
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

  function noteCloseCode(ev) {
    const code = ev && Number(ev.code);
    if (Number.isFinite(code) && code > 0) closeCode = code;
  }

  function holdFor(kind) {
    hold = kind;
    emit(kind);
    return new Promise((resolve) => {
      releaseHold = () => {
        releaseHold = null;
        resolve();
      };
    });
  }

  async function waitForOnline() {
    if (online || stopped) return;
    await new Promise((resolve) => {
      releaseOnline = () => {
        releaseOnline = null;
        resolve();
      };
    });
  }

  function setOffline() {
    if (!online && !socket) return;
    online = false;
    if (hold) return;
    emit("device_offline");
    if (socket) {
      skipAccounting = true;
      const ws = socket;
      try { ws.close(); } catch { /* onclose continues */ }
      return;
    }
    if (waitFinish) waitFinish();
  }

  function setOnline() {
    const wasOffline = !online;
    online = true;
    if (releaseOnline) releaseOnline();
    if (hold || !wasOffline) return;
    failures = 0;
    emit("connecting", { text: "網路恢復，重新連線中" });
    if (socket) {
      skipAccounting = true;
      const ws = socket;
      try { ws.close(); } catch { /* onclose continues */ }
    } else if (waitFinish) {
      waitFinish();
    }
  }

  function abandonSocket(ws) {
    // A dead TCP connection may not fire onclose for a long time. Unbind and
    // let this loop open the next socket itself.
    if (!ws) return;
    detach(ws);
    if (socket === ws) socket = null;
    try { ws.close(); } catch { /* already dead */ }
    const finish = cancelWait;
    if (finish) finish();
  }

  function checkStale() {
    if (stopped || !socket || !lastMessageAt || pageHidden()) return;
    if (clock() - lastMessageAt <= staleAfter) return;
    emit("reconnecting", { text: "連線不穩，重新連線中", attempt: Math.max(1, failures + 1) });
    abandonSocket(socket);
  }

  function armWatch() {
    if (watchId != null) return;
    watchId = scheduleFn(checkStale, watchMs);
  }

  function disarmWatch() {
    if (watchId == null) return;
    const id = watchId;
    watchId = null;
    try { cancelScheduleFn(id); } catch { /* already cleared */ }
  }

  function onOfflineEvent() { setOffline(); }
  function onOnlineEvent() { setOnline(); }

  function bindNetwork() {
    if (!net || typeof net.addEventListener !== "function") return;
    net.addEventListener("offline", onOfflineEvent);
    net.addEventListener("online", onOnlineEvent);
  }

  function unbindNetwork() {
    if (!net || typeof net.removeEventListener !== "function") return;
    net.removeEventListener("offline", onOfflineEvent);
    net.removeEventListener("online", onOnlineEvent);
  }

  async function afterClose() {
    if (stopped) return;
    if (!online) {
      skipAccounting = false;
      emit("device_offline");
      await waitForOnline();
      if (stopped) return;
      if (hold) {
        await holdFor(hold);
        return;
      }
      failures = 0;
      emit("connecting", { text: "網路恢復，重新連線中" });
      return;
    }
    if (skipAccounting) {
      skipAccounting = false;
      return;
    }
    if (closeCode === 4401 || closeReason === "link_invalid") {
      await holdFor("link_invalid");
      return;
    }
    if (closeCode === 1008 || closeReason === "rejected") {
      await holdFor("rejected");
      return;
    }
    if (closeReason === "ended" || (closeCode === 4404 && closeReason !== "unknown_or_ended" && closeReason !== "not_open" && closeReason !== "full")) {
      await holdFor("ended");
      return;
    }
    if (closeReason === "unknown_or_ended" || closeReason === "not_open") {
      if (!waitingSince) waitingSince = clock();
      const delay = waitingDelayMs(clock() - waitingSince, roll(), pageHidden());
      emit("waiting_room", { nextRetryAt: clock() + delay });
      await waitMs(delay);
      return;
    }
    if (closeReason === "full" || closeCode === 1013) {
      waitingSince = 0;
      const delay = fullDelayMs(roll());
      emit("room_full", { nextRetryAt: clock() + delay });
      await waitMs(delay);
      return;
    }
    if (deferredThisAttempt) {
      const wait = pendingWait > 0 ? pendingWait : deferredRetryMs(1000, roll());
      pendingWait = 0;
      await waitMs(wait);
      return;
    }
    failures += 1;
    waitingSince = 0;
    const hidden = pageHidden();
    const delay = reconnectDelayMs(failures, roll(), hidden);
    const kind = failures >= 6 ? "unreachable" : (sawHello ? "reconnecting" : "connecting");
    emit(kind, { attempt: failures, nextRetryAt: clock() + delay });
    await waitMs(delay);
  }

  async function loop() {
    if (loopRunning) return;
    loopRunning = true;
    bindNetwork();
    armWatch();
    try {
      while (!stopped) {
        if (hold) {
          await holdFor(hold);
          if (stopped) break;
          hold = null;
        }
        if (!online) {
          emit("device_offline");
          await waitForOnline();
          if (stopped) break;
          failures = 0;
          emit("connecting", { text: "網路恢復，重新連線中" });
          continue;
        }
        closeReason = "";
        closeCode = 0;
        deferredThisAttempt = false;
        if (waitingSince) emit("waiting_room");
        else if (!replaceOnBackfill && failures === 0) emit("connecting");
        else if (failures >= 6) emit("unreachable", { attempt: failures });
        else if (sawHello && failures > 0) emit("reconnecting", { attempt: failures });
        else if (!replaceOnBackfill) emit("connecting");
        let ws = null;
        try {
          ws = opener(address());
        } catch {
          ws = null;
        }
        if (!ws) {
          await afterClose();
          continue;
        }
        socket = ws;
        // New handshake. The previous socket's last message must not close this one.
        lastMessageAt = clock();
        await new Promise((resolve) => {
          let settled = false;
          const finish = () => {
            if (settled) return;
            settled = true;
            if (cancelWait === finish) cancelWait = null;
            resolve();
          };
          cancelWait = finish;
          ws.onopen = () => {
            markMessage();
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
            if (data.type === "room") {
              if (onHost) onHost(data);
              return;
            }
            if (data.type === "room_unavailable") {
              closeReason = String(data.reason || "");
              try { ws.close(); } catch { finish(); }
              return;
            }
            if (data.type === "hello") {
              failures = 0;
              sawHello = true;
              waitingSince = 0;
              const latest = Number(data.latest_cursor);
              const epoch = data.epoch == null ? null : Number(data.epoch);
              const cursorBehind = Number.isFinite(latest) && latest < cursor;
              const epochChanged = epoch != null && seenEpoch != null && epoch !== seenEpoch;
              const replaying = replaceOnBackfill;
              // A same-epoch gap can still be deferred. Without this, the cursor
              // jumps to latest and that hole is never replayed.
              const needsReplace = cursorBehind || epochChanged || replaying || data.backfill_deferred === true;
              if (onHost && Object.prototype.hasOwnProperty.call(data, "host_live")) {
                onHost({ type: "room", room_id: data.room_id, live: !!data.host_live });
              }
              if (needsReplace) {
                const deferred = data.backfill_deferred === true;
                const hasBackfill = Array.isArray(data.backfill);
                // Rate limit delays replay only. This socket stays up for live captions.
                if (deferred && ws === socket) {
                  replaceOnBackfill = true;
                  const hinted = Number(data.retry_after_ms != null ? data.retry_after_ms : data.retry_after);
                  const wait = deferredRetryMs(Number.isFinite(hinted) ? hinted : 1000, roll());
                  emit("live");
                  for (const item of data.history || []) deliver(item);
                  for (const item of data.events || []) deliver(item);
                  scheduleBackfill(wait);
                  return;
                }
                // No payload yet, and this was not a deferred live hello: ask again.
                if (deferred || (!hasBackfill && !replaying)) {
                  replaceOnBackfill = true;
                  deferredThisAttempt = true;
                  const hinted = Number(data.retry_after_ms != null ? data.retry_after_ms : data.retry_after);
                  pendingWait = deferredRetryMs(Number.isFinite(hinted) ? hinted : 1000, roll());
                  try { ws.close(); } catch { /* reconnect below */ }
                  return;
                }
                acceptReplace(data);
                return;
              }
              if (epoch != null && Number.isFinite(epoch)) seenEpoch = epoch;
              cursor = noteCursor(cursor, data.latest_cursor);
              emit("live");
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
          ws.onclose = (ev) => {
            noteCloseCode(ev);
            finish();
          };
          ws.onerror = () => { try { ws.close(); } catch { finish(); } };
        });
        detach(ws);
        if (socket === ws) socket = null;
        if (stopped) break;
        await afterClose();
      }
    } finally {
      loopRunning = false;
      disarmWatch();
      unbindNetwork();
      if (!finished) {
        finished = true;
        resolveDone();
      }
    }
  }

  function restart() {
    stopped = false;
    hold = null;
    failures = 0;
    closeReason = "";
    closeCode = 0;
    if (releaseHold) releaseHold();
    if (!loopRunning) {
      armWatch();
      loop();
      return;
    }
    if (socket) {
      skipAccounting = true;
      const ws = socket;
      try { ws.close(); } catch { /* onclose continues */ }
      return;
    }
    if (waitFinish) waitFinish();
  }

  loop();
  return {
    done,
    get cursor() { return cursor; },
    nudge() {
      // The room has ended. Coming back to the tab must not start polling again.
      if (hold === "ended") return;
      if (stopped || !loopRunning || hold) {
        restart();
        return;
      }
      failures = 0;
      if (socket && lastMessageAt && clock() - lastMessageAt > staleAfter) {
        abandonSocket(socket);
        return;
      }
      // Backoff uses the timer. A live socket that is still receiving stays up.
      if (timer && waitFinish) waitFinish();
    },
    restart,
    stop() {
      stopped = true;
      backfillGen += 1;
      clearBackfillTimer();
      clearExtraHelloTimer();
      if (extraSocket) {
        const extra = extraSocket;
        extraSocket = null;
        detach(extra);
        try { extra.close(); } catch { /* already closed */ }
      }
      hold = null;
      disarmWatch();
      unbindNetwork();
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      const cancel = cancelWait;
      cancelWait = null;
      const waiting = waitFinish;
      waitFinish = null;
      if (socket) {
        const ws = socket;
        socket = null;
        detach(ws);
        try { ws.close(); } catch { /* already closed */ }
      }
      if (releaseHold) releaseHold();
      if (releaseOnline) releaseOnline();
      if (waiting && waiting !== cancel) waiting();
      if (cancel) cancel();
    },
  };
}
