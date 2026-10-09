import assert from "node:assert/strict";
import { connectRoom, createCaptionView, liveTail, mergeCaptionUpdate } from "../app/static/room_client.js";
import { EXPIRY_NOTE, expiryNotice } from "../app/static/room_view.js";

function tick() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

const items = new Map();
function apply(item) {
  const prev = items.get(item.id);
  if (prev && (prev.version || 1) >= (item.version || 1)) return;
  items.set(item.id, item);
}

apply({ id: "room:a:20", session_id: "a", session_ord: 1, seq: 20, version: 1, zh: "舊" });
apply({ id: "room:b:1", session_id: "b", session_ord: 2, seq: 1, version: 1, zh: "新" });
assert.equal(liveTail(items).zh, "新");
apply({ id: "room:a:20", session_id: "a", session_ord: 1, seq: 20, version: 2, zh: "舊", en: "old" });
assert.equal(liveTail(items).zh, "新");
assert.equal(liveTail(items, false, "room:a:20").en, "old");

const events = [];
const sockets = [];
function fakeSocket(address) {
  const ws = {
    address,
    readyState: 1,
    sent: [],
    close() { this.closed = true; this.onclose?.(); },
    send(body) { this.sent.push(body); },
  };
  sockets.push(ws);
  return ws;
}

const conn = connectRoom({
  room: "class",
  url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
  openSocket: fakeSocket,
  sleep: () => Promise.resolve(),
  onState: () => {},
  onEvent: (item) => events.push(item),
  onGap: () => events.push({ gap: true }),
});
await tick();
const first = sockets[0];
first.onopen();
first.onmessage({ data: "not-json" });
first.onmessage({ data: JSON.stringify({ type: "ping" }) });
assert.equal(JSON.parse(first.sent[0]).type, "pong");
first.onmessage({
  data: JSON.stringify({
    type: "hello",
    latest_cursor: 3,
    history: [
      { id: "class:a:20", session_id: "a", session_ord: 1, seq: 20, version: 1, cursor: 1, zh: "舊會話" },
      { id: "class:b:1", session_id: "b", session_ord: 2, seq: 1, version: 1, cursor: 2, zh: "新會話" },
    ],
  }),
});
first.onmessage({
  data: JSON.stringify({
    type: "caption",
    id: "class:a:20",
    session_id: "a",
    session_ord: 1,
    seq: 20,
    version: 2,
    cursor: 3,
    zh: "舊會話",
    en: "old session",
  }),
});
assert.equal(events.filter((item) => item.zh === "新會話").length, 1);
assert.equal(events.find((item) => item.id === "class:a:20" && item.version === 2).en, "old session");
const shown = new Map(events.filter((item) => item.id).map((item) => [item.id, item]));
assert.equal(liveTail(shown).zh, "新會話");

first.onclose();
await tick();
assert.match(sockets[1].address, /cursor=3/);
sockets[1].onopen();
sockets[1].onmessage({
  data: JSON.stringify({
    type: "hello",
    latest_cursor: 4,
    gap: true,
    events: [{ id: "class:b:1", session_id: "b", session_ord: 2, seq: 1, version: 2, cursor: 4, zh: "新會話", en: "new" }],
  }),
});
assert.equal(events.some((item) => item.gap), true);
assert.equal(events.filter((item) => item.id === "class:b:1" && item.version === 2).length, 1);
sockets[1].onmessage({
  data: JSON.stringify({ id: "class:b:1", session_id: "b", version: 2, seq: 1, zh: "新會話", en: "new" }),
});
assert.equal(events.filter((item) => item.id === "class:b:1" && item.version === 2).length, 1);

const view = new Map(events.filter((item) => item.id).map((item) => [item.id, item]));
assert.equal(liveTail(view, false, "class:b:1").zh, "新會話");
assert.equal(liveTail(view, false, "class:b:1").id, "class:b:1");
assert.notEqual(liveTail(view, false, "class:b:1").id, "class:a:20");

sockets[1].onmessage({
  data: JSON.stringify({ id: "class:b:1", session_id: "b", version: 2, seq: 99, cursor: 5, zh: "新會話", en: "new" }),
});
assert.equal(events.filter((item) => item.id === "class:b:1" && item.version === 2).length, 1);
assert.equal(conn.cursor, 5);

conn.stop();
assert.equal(sockets[1].closed, true);
assert.equal(sockets[1].onmessage, null);
if (sockets[1].onmessage) sockets[1].onmessage({ data: JSON.stringify({ type: "ping" }) });
assert.equal(sockets[1].sent.length, 0);
await conn.done;

const nudged = [];
const nudgeConn = connectRoom({
  room: "class",
  url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
  openSocket(address) {
    const ws = fakeSocket(address);
    nudged.push(ws);
    return ws;
  },
  onState: () => {},
  onEvent: () => {},
});
await tick();
assert.equal(nudged.length, 1);
nudgeConn.nudge();
await tick();
assert.equal(nudged.length, 1);
assert.equal(typeof nudged[0].onmessage, "function");
nudged[0].onmessage({ data: JSON.stringify({ type: "ping" }) });
assert.equal(JSON.parse(nudged[0].sent[0]).type, "pong");
nudgeConn.stop();
await nudgeConn.done;

const resumed = [];
const resumeConn = connectRoom({
  room: "class",
  url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
  openSocket(address) {
    const ws = fakeSocket(address);
    resumed.push(ws);
    return ws;
  },
  onState: () => {},
  onEvent: () => {},
});
await tick();
resumed[0].close();
await tick();
resumeConn.nudge();
await tick();
assert.equal(resumed.length, 2);
assert.equal(typeof resumed[1].onmessage, "function");
await new Promise((resolve) => setTimeout(resolve, 900));
assert.equal(typeof resumed[1].onmessage, "function");
resumed[1].onmessage({ data: JSON.stringify({ type: "ping" }) });
assert.equal(JSON.parse(resumed[1].sent[0]).type, "pong");
resumeConn.stop();
await resumeConn.done;

const kept = { id: "class:s:1", version: 2, zh: "中", en: "old" };
assert.equal(mergeCaptionUpdate(kept, { id: "class:s:1", version: 1, zh: "中", en: "" }), kept);
assert.equal(mergeCaptionUpdate(kept, { id: "class:s:1", version: 2, zh: "中", en: "same" }), kept);
assert.equal(mergeCaptionUpdate(kept, { id: "", version: 3, en: "nope" }), kept);
assert.equal(mergeCaptionUpdate(null, { id: "", version: 1 }), null);
const newer = mergeCaptionUpdate(kept, { id: "class:s:1", version: 3, zh: "中", en: "new" });
assert.equal(newer.en, "new");
assert.equal(newer.version, 3);
assert.equal(mergeCaptionUpdate(null, { id: "class:s:2", zh: "下一句" }).zh, "下一句");

const removed = [];
const cleared = [];
const resetNotes = [];
const live = [];
const deleteSockets = [];
const deleteConn = connectRoom({
  room: "class",
  url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
  openSocket(address) {
    const ws = fakeSocket(address);
    deleteSockets.push(ws);
    return ws;
  },
  onState: () => {},
  onEvent: (item) => live.push(item),
  onDelete: (item) => removed.push(item),
  onClear: (item) => cleared.push(item),
  onReset: () => resetNotes.push("reset"),
});
await tick();
deleteSockets[0].onopen();
deleteSockets[0].onmessage({
  data: JSON.stringify({ type: "hello", latest_cursor: 1, history: [{ id: "class:s:1", session_id: "s", seq: 1, version: 1, cursor: 1, zh: "留下" }] }),
});
deleteSockets[0].onmessage({
  data: JSON.stringify({ type: "caption_deleted", id: "class:s:1", session_id: "s", seq: 1, version: 9, cursor: 2, zh: "" }),
});
assert.equal(removed.length, 1);
assert.equal(removed[0].id, "class:s:1");
assert.equal(live.filter((item) => item.type === "caption_deleted").length, 0);
assert.equal(live.filter((item) => item.zh === "留下").length, 1);
deleteSockets[0].onmessage({
  data: JSON.stringify({ type: "captions_cleared", room_id: "class", epoch: 2, cursor: 3 }),
});
assert.equal(cleared.length, 1);
assert.equal(live.filter((item) => item.type === "captions_cleared").length, 0);
assert.equal(deleteConn.cursor, 3);
deleteConn.stop();
await deleteConn.done;

const captionView = createCaptionView();
captionView.apply({ id: "class:s:1", session_id: "s", seq: 1, version: 1, zh: "甲" });
captionView.apply({ type: "caption_deleted", id: "class:s:1", session_id: "s", seq: 1 });
captionView.apply({ id: "class:s:1", session_id: "s", seq: 1, version: 4, zh: "甲復活" });
assert.equal(captionView.items.has("class:s:1"), false);
captionView.apply({ id: "class:s:2", session_id: "s", seq: 2, version: 1, zh: "乙" });
captionView.apply({ type: "captions_cleared" });
assert.equal(captionView.items.size, 0);
captionView.apply({ id: "class:s:2", session_id: "s", seq: 2, version: 3, zh: "乙復活" });
assert.equal(captionView.items.has("class:s:2"), false);
captionView.reset();
captionView.apply({ id: "class:s:2", session_id: "s", seq: 2, version: 1, zh: "乙" });
assert.equal(captionView.items.get("class:s:2").zh, "乙");

const rewindSockets = [];
const rewindEvents = [];
const rewindConn = connectRoom({
  room: "class",
  url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
  openSocket(address) {
    const ws = fakeSocket(address);
    rewindSockets.push(ws);
    return ws;
  },
  sleep: () => Promise.resolve(),
  onState: () => {},
  onEvent: (item) => rewindEvents.push(item),
  onReset: () => resetNotes.push("rewind"),
});
await tick();
rewindSockets[0].onopen();
rewindSockets[0].onmessage({
  data: JSON.stringify({
    type: "hello",
    latest_cursor: 50,
    history: [{ id: "class:s:9", session_id: "s", seq: 9, version: 1, cursor: 50, zh: "舊游標" }],
  }),
});
assert.equal(rewindConn.cursor, 50);
rewindSockets[0].onclose();
await tick();
assert.match(rewindSockets[1].address, /cursor=50/);
rewindSockets[1].onopen();
rewindSockets[1].onmessage({
  data: JSON.stringify({
    type: "hello",
    latest_cursor: 2,
    history: [],
    events: [],
  }),
});
await tick();
assert.equal(resetNotes.includes("rewind"), false);
assert.match(rewindSockets.at(-1).address, /cursor=0/);
const resumedSocket = rewindSockets.at(-1);
resumedSocket.onopen();
resumedSocket.onmessage({
  data: JSON.stringify({
    type: "hello",
    latest_cursor: 2,
    history: [{ id: "class:s:1", session_id: "s", seq: 1, version: 1, cursor: 2, zh: "重來" }],
  }),
});
assert.ok(resetNotes.includes("rewind"));
assert.equal(rewindEvents.filter((item) => item.zh === "重來").length, 1);
assert.equal(rewindConn.cursor, 2);
rewindConn.stop();
await rewindConn.done;

async function testEpochResetRequestsReplayAndGapBackfill() {
  const sockets = [];
  const events = [];
  const backfills = [];
  const resets = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep: () => Promise.resolve(),
    onState: () => {},
    onEvent: (item) => events.push(item),
    onBackfill: (rows) => backfills.push(rows),
    onReset: () => resets.push("reset"),
  });
  await tick();
  sockets[0].onopen();
  sockets[0].onmessage({
    data: JSON.stringify({
      type: "hello",
      epoch: 4,
      latest_cursor: 3,
      history: [{ id: "class:s:1", session_id: "s", seq: 1, version: 2, cursor: 3, zh: "舊" }],
    }),
  });
  assert.equal(conn.cursor, 3);
  sockets[0].onclose();
  await tick();
  assert.match(sockets[1].address, /cursor=3/);
  sockets[1].onopen();
  sockets[1].onmessage({
    data: JSON.stringify({
      type: "hello",
      epoch: 9,
      latest_cursor: 1,
      history: [],
      events: [],
      gap: true,
      backfill_deferred: true,
      retry_after_ms: 1000,
    }),
  });
  await tick();
  assert.deepEqual(resets, []);
  assert.equal(backfills.length, 0);
  assert.match(sockets.at(-1).address, /cursor=0/);
  assert.match(sockets.at(-1).address, /replay=1/);
  const replay = sockets.at(-1);
  replay.onopen();
  replay.onmessage({
    data: JSON.stringify({
      type: "hello",
      epoch: 9,
      latest_cursor: 2,
      backfill: [
        { id: "class:s:1", session_id: "s", seq: 1, version: 2, cursor: 1, zh: "甲" },
        { id: "class:s:2", session_id: "s", seq: 2, version: 1, cursor: 2, zh: "乙" },
      ],
      history: [{ id: "class:s:1", session_id: "s", seq: 1, version: 2, cursor: 1, zh: "甲" }],
    }),
  });
  assert.ok(resets.includes("reset"));
  assert.equal(backfills.at(-1).map((item) => item.zh).join(","), "甲,乙");
  assert.equal(events.filter((item) => item.zh === "甲").length, 1);
  replay.onmessage({
    data: JSON.stringify({ id: "class:s:1", session_id: "s", seq: 1, version: 2, zh: "甲" }),
  });
  assert.equal(events.filter((item) => item.zh === "甲").length, 1);
  conn.stop();
  await conn.done;
}
await testEpochResetRequestsReplayAndGapBackfill();

async function testRoomUnavailableDoesNotResetBackoff() {
  const sockets = [];
  const waits = [];
  const states = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep: (ms) => {
      waits.push(ms);
      return Promise.resolve();
    },
    random: () => 0,
    onState: (detail) => states.push(detail),
    onEvent: () => {},
  });
  await tick();
  for (let i = 0; i < 3; i += 1) {
    const ws = sockets[i];
    assert.ok(ws);
    ws.onopen();
    ws.onmessage({ data: JSON.stringify({ type: "room_unavailable", reason: "unknown_or_ended" }) });
    await tick();
  }
  assert.deepEqual(waits.slice(0, 3), [3500, 3500, 3500]);
  const rendered = states.map((detail) => (detail && detail.text) || String(detail)).join(" | ");
  assert.ok(states.some((detail) => detail && detail.kind === "waiting_room"), rendered);
  assert.ok(rendered.includes("等待主持人"), rendered);
  assert.equal(rendered.includes("斷線"), false, rendered);
  assert.equal(rendered.includes("服務離線"), false, rendered);
  assert.equal(rendered.includes("房間已結束"), false, rendered);
  conn.stop();
  await conn.done;
}
await testRoomUnavailableDoesNotResetBackoff();

async function testStaleNudgeReconnectsFromLastMessageTime() {
  let now = 10000;
  const sockets = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    now: () => now,
    staleMs: 1000,
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep: () => Promise.resolve(),
    onState: () => {},
    onEvent: () => {},
  });
  await tick();
  sockets[0].onopen();
  sockets[0].onmessage({ data: JSON.stringify({ type: "ping" }) });
  now = 10500;
  conn.nudge();
  await tick();
  assert.equal(sockets.length, 1);
  assert.equal(sockets[0].closed, undefined);
  now = 12000;
  conn.nudge();
  await tick();
  assert.equal(sockets[0].closed, true);
  assert.match(sockets.at(-1).address, /cursor=/);
  conn.stop();
  await conn.done;
}
await testStaleNudgeReconnectsFromLastMessageTime();

async function testExpiredCaptionIsRemovedAndCanBeShownAgain() {
  const view = createCaptionView();
  view.apply({ id: "class:s:1", session_id: "s", seq: 1, version: 2, zh: "舊" });
  view.apply({ id: "class:s:2", session_id: "s", seq: 2, version: 1, zh: "留下" });
  view.apply({ type: "captions_expired", room_id: "class", ids: ["class:s:1"] });
  assert.equal(view.items.has("class:s:1"), false);
  assert.equal(view.items.get("class:s:2").zh, "留下");
  view.apply({ id: "class:s:1", session_id: "s", seq: 1, version: 1, zh: "再來" });
  assert.equal(view.items.get("class:s:1").zh, "再來");

  const events = [];
  const removed = [];
  const expired = [];
  const notices = [];
  const screen = new Map();
  const sockets = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep: () => Promise.resolve(),
    onState: () => {},
    onEvent: (item) => {
      events.push(item);
      if (item && item.id) screen.set(item.id, item);
    },
    onDelete: (item) => {
      removed.push(item);
      if (item && item.id) screen.delete(item.id);
    },
    onExpire: (item) => {
      expired.push(item);
      notices.push(expiryNotice(item && item.ids, screen));
    },
  });
  await tick();
  sockets[0].onopen();
  sockets[0].onmessage({
    data: JSON.stringify({
      type: "hello",
      latest_cursor: 2,
      history: [
        { id: "class:s:1", session_id: "s", seq: 1, version: 1, cursor: 1, zh: "舊" },
        { id: "class:s:2", session_id: "s", seq: 2, version: 1, cursor: 2, zh: "留下" },
      ],
    }),
  });
  sockets[0].onmessage({
    data: JSON.stringify({ type: "captions_expired", room_id: "class", ids: ["class:s:1"] }),
  });
  assert.equal(expired.length, 1);
  assert.deepEqual(expired[0].ids, ["class:s:1"]);
  assert.equal(notices[0], EXPIRY_NOTE);
  assert.equal(removed.length, 1);
  assert.equal(removed[0].id, "class:s:1");
  assert.equal(events.filter((item) => item.type === "captions_expired").length, 0);
  sockets[0].onmessage({
    data: JSON.stringify({ id: "class:s:1", session_id: "s", seq: 1, version: 1, cursor: 3, zh: "再來" }),
  });
  assert.equal(events.filter((item) => item.zh === "再來").length, 1);
  conn.stop();
  await conn.done;
}
await testExpiredCaptionIsRemovedAndCanBeShownAgain();
console.log("room client ok");
