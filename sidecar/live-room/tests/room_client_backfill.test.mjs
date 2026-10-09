// A deferred hello must not clear captions already on screen. The retry wait
// is retry_after plus jitter, never shorter than retry_after.

import assert from "node:assert/strict";
import { connectRoom, createCaptionView, deferredRetryMs } from "../app/static/room_client.js";

function tick() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function fakeSocket(address) {
  return {
    address,
    readyState: 1,
    sent: [],
    close() { this.closed = true; this.onclose?.(); },
    send(body) { this.sent.push(body); },
  };
}

assert.equal(deferredRetryMs(2000, 0), 2000);
assert.equal(deferredRetryMs(2000, 1), 3000);
assert.equal(deferredRetryMs(2000, 0.5), 2500);
assert.ok(deferredRetryMs(0, 0) >= 500);
assert.ok(deferredRetryMs(0, 1) <= 1000);
assert.ok(deferredRetryMs(2000, 0) <= deferredRetryMs(2000, 1));

const shown = [];
const resets = [];
const view = createCaptionView();
view.apply({ id: "class:s:9", session_id: "s", seq: 9, version: 1, zh: "還在" });

function paint(why) {
  const text = [...view.items.values()].map((item) => item.zh).join(",");
  shown.push(why + ":" + text);
  assert.notEqual(text, "", "screen painted blank");
}
paint("seed");

const sockets = [];
const waits = [];
const conn = connectRoom({
  room: "class",
  url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
  openSocket(address) {
    const ws = fakeSocket(address);
    sockets.push(ws);
    return ws;
  },
  sleep(ms) {
    waits.push(ms);
    return Promise.resolve();
  },
  random: () => 1,
  onState: () => {},
  onEvent(item) {
    view.apply(item);
    paint("event");
  },
  onBackfill(rows) {
    view.replace(rows);
    paint("backfill");
  },
  onReset() {
    resets.push("reset");
    view.reset();
  },
});

await tick();
sockets[0].onopen();
sockets[0].onmessage({
  data: JSON.stringify({
    type: "hello",
    epoch: 1,
    latest_cursor: 4,
    history: [{ id: "class:s:9", session_id: "s", seq: 9, version: 1, cursor: 4, zh: "還在" }],
  }),
});
sockets[0].onclose();
await tick();
sockets[1].onopen();
sockets[1].onmessage({
  data: JSON.stringify({
    type: "hello",
    epoch: 2,
    latest_cursor: 4,
    backfill_deferred: true,
    retry_after_ms: 2000,
    history: [],
    events: [],
  }),
});
await tick();
assert.deepEqual(resets, []);
assert.equal(view.items.get("class:s:9").zh, "還在");
assert.equal(shown.some((row) => row.endsWith(":")), false);
assert.equal(waits.at(-1), deferredRetryMs(2000, 1));
const replay = sockets.at(-1);
assert.match(replay.address, /replay=1/);
assert.match(replay.address, /cursor=0/);
replay.onopen();
replay.onmessage({
  data: JSON.stringify({
    type: "hello",
    epoch: 2,
    latest_cursor: 4,
    backfill: [
      { id: "class:s:1", session_id: "s", seq: 1, version: 1, cursor: 1, zh: "甲" },
      { id: "class:s:2", session_id: "s", seq: 2, version: 1, cursor: 2, zh: "乙" },
    ],
  }),
});
assert.ok(resets.length >= 1);
assert.equal([...view.items.values()].map((item) => item.zh).join(","), "甲,乙");
assert.equal(shown.some((row) => row.endsWith(":")), false);
assert.match(shown.at(-1), /甲,乙/);
conn.stop();
await conn.done;

async function testSameEpochGapDeferredRetriesUntilBackfill() {
  const sockets = [];
  const resets = [];
  const view = createCaptionView();
  view.apply({ id: "class:s:9", session_id: "s", seq: 9, version: 1, zh: "還在" });
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep: () => Promise.resolve(),
    random: () => 0,
    onState: () => {},
    onEvent(item) { view.apply(item); },
    onBackfill(rows) { view.replace(rows); },
    onReset() {
      resets.push("reset");
      view.reset();
    },
  });
  await tick();
  sockets[0].onopen();
  sockets[0].onmessage({
    data: JSON.stringify({
      type: "hello",
      epoch: 3,
      latest_cursor: 10,
      history: [{ id: "class:s:9", session_id: "s", seq: 9, version: 1, cursor: 10, zh: "還在" }],
    }),
  });
  sockets[0].onclose();
  await tick();
  assert.equal(conn.cursor, 10);
  const stalled = sockets.at(-1);
  stalled.onopen();
  stalled.onmessage({
    data: JSON.stringify({
      type: "hello",
      epoch: 3,
      latest_cursor: 400,
      gap: true,
      backfill_deferred: true,
      retry_after_ms: 1000,
      history: [],
      events: [],
    }),
  });
  await tick();
  assert.deepEqual(resets, []);
  assert.equal(view.items.get("class:s:9").zh, "還在");
  assert.equal(conn.cursor, 10);
  assert.notEqual(stalled.closed, true);
  stalled.onmessage({
    data: JSON.stringify({ id: "class:s:live", session_id: "s", seq: 50, version: 1, zh: "即時" }),
  });
  assert.equal(view.items.get("class:s:live").zh, "即時");
  assert.equal(conn.cursor, 10);
  const replay = sockets.at(-1);
  assert.notEqual(replay, stalled);
  assert.match(replay.address, /replay=1/);
  assert.match(replay.address, /cursor=0/);
  replay.onopen();
  const tail = [];
  for (let seq = 1; seq <= 200; seq += 1) {
    tail.push({ id: "class:s:" + seq, session_id: "s", seq, version: 1, cursor: seq, zh: "L" + seq });
  }
  replay.onmessage({
    data: JSON.stringify({
      type: "hello",
      epoch: 3,
      latest_cursor: 400,
      backfill: tail,
    }),
  });
  assert.ok(resets.length >= 1);
  assert.equal(view.items.size, 80);
  assert.equal(view.items.has("class:s:1"), false);
  assert.equal(view.items.get("class:s:200").zh, "L200");
  conn.stop();
  await conn.done;
}
await testSameEpochGapDeferredRetriesUntilBackfill();

function deferredHello(extra) {
  return {
    data: JSON.stringify({
      type: "hello",
      epoch: 1,
      latest_cursor: 4,
      backfill_deferred: true,
      retry_after_ms: 1500,
      history: [{ id: "class:s:1", session_id: "s", seq: 1, version: 1, cursor: 1, zh: "歷史" }],
      events: [{ id: "class:s:2", session_id: "s", seq: 2, version: 1, cursor: 2, zh: "事件" }],
      ...extra,
    }),
  };
}

async function openDeferred(states, view) {
  const sockets = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class&cid=phone-a",
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep: () => Promise.resolve(),
    random: () => 0,
    onState(detail) { states.push(detail.kind); },
    onEvent(item) { view.apply(item); },
    onBackfill(rows) { view.replace(rows); },
    onReset() { view.reset(); },
  });
  await tick();
  const main = sockets[0];
  main.onopen();
  states.length = 0;
  main.onmessage(deferredHello());
  await tick();
  return { conn, sockets, main, side: sockets.at(-1) };
}

async function testDeferredHelloStaysLiveAndKeepsHistory() {
  const states = [];
  const view = createCaptionView();
  const { conn, main, side } = await openDeferred(states, view);
  assert.ok(states.includes("live"), "deferred hello must enter live");
  assert.equal(view.items.get("class:s:1").zh, "歷史");
  assert.equal(view.items.get("class:s:2").zh, "事件");
  assert.notEqual(main.closed, true);
  assert.notEqual(side, main);
  assert.match(side.address, /replay=1/);
  assert.match(side.address, /cursor=0/);
  assert.match(side.address, /supplement=1/);
  assert.doesNotMatch(main.address, /supplement=1/);
  side.onopen();
  side.onmessage({
    data: JSON.stringify({ id: "class:s:live", session_id: "s", seq: 9, version: 1, zh: "副線即時" }),
  });
  assert.equal(view.items.get("class:s:live").zh, "副線即時");
  conn.stop();
  await conn.done;
}

async function testSideDeferReschedulesWithoutClosingMain() {
  const states = [];
  const view = createCaptionView();
  const { conn, sockets, main, side } = await openDeferred(states, view);
  side.onopen();
  const count = sockets.length;
  side.onmessage({
    data: JSON.stringify({
      type: "hello",
      epoch: 1,
      backfill_deferred: true,
      retry_after_ms: 1500,
    }),
  });
  await tick();
  assert.notEqual(main.closed, true, "a deferred side socket must not close the live socket");
  assert.ok(sockets.length > count, "a deferred side socket must schedule another backfill");
  const retry = sockets.at(-1);
  assert.match(retry.address, /replay=1/);
  assert.match(retry.address, /supplement=1/);
  assert.notEqual(retry.closed, true);
  retry.onopen();
  retry.onmessage({
    data: JSON.stringify({
      type: "hello",
      epoch: 1,
      latest_cursor: 8,
      backfill: [{ id: "class:s:3", session_id: "s", seq: 3, version: 1, cursor: 3, zh: "補" }],
    }),
  });
  assert.equal(retry.closed, true, "the backfill socket closes when its replacement is applied");
  assert.notEqual(main.closed, true);
  assert.equal(view.items.get("class:s:3").zh, "補");
  conn.stop();
  await conn.done;
}

async function testMainBackfillClosesTheSideSocket() {
  const states = [];
  const view = createCaptionView();
  const { conn, main, side } = await openDeferred(states, view);
  side.onopen();
  assert.notEqual(side.closed, true);
  main.onmessage({
    data: JSON.stringify({
      type: "hello",
      epoch: 1,
      latest_cursor: 8,
      backfill: [{ id: "class:s:4", session_id: "s", seq: 4, version: 1, cursor: 4, zh: "主線補完" }],
    }),
  });
  assert.equal(side.closed, true, "acceptReplace on the live socket closes the backfill socket");
  assert.notEqual(main.closed, true);
  assert.equal(view.items.get("class:s:4").zh, "主線補完");
  conn.stop();
  await conn.done;
}

async function testStopClosesTheBackfillSocket() {
  const states = [];
  const view = createCaptionView();
  const { conn, main, side } = await openDeferred(states, view);
  side.onopen();
  assert.notEqual(side.closed, true);
  conn.stop();
  assert.equal(side.closed, true, "stop() must close the backfill socket");
  assert.equal(main.closed, true);
  await conn.done;
}

async function testExtraHelloTimeoutReschedules() {
  const states = [];
  const view = createCaptionView();
  const sockets = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class&cid=phone-a",
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep: () => Promise.resolve(),
    random: () => 0,
    extraHelloMs: 30,
    onState(detail) { states.push(detail.kind); },
    onEvent(item) { view.apply(item); },
    onBackfill(rows) { view.replace(rows); },
    onReset() { view.reset(); },
  });
  await tick();
  const main = sockets[0];
  main.onopen();
  main.onmessage(deferredHello());
  await tick();
  const side = sockets.at(-1);
  assert.notEqual(side, main);
  side.onopen();
  assert.notEqual(side.closed, true);
  const started = Date.now();
  while (!side.closed) {
    assert.ok(Date.now() - started < 5000, "supplement hello timeout did not fire");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.notEqual(main.closed, true, "the live socket stays up when the supplement times out");
  const retry = sockets.at(-1);
  assert.notEqual(retry, side, "a supplement with no hello must not stick replaceOnBackfill");
  assert.match(retry.address, /supplement=1/);
  assert.equal(view.items.get("class:s:1").zh, "歷史");
  retry.onopen();
  retry.onmessage({
    data: JSON.stringify({
      type: "hello",
      epoch: 1,
      latest_cursor: 8,
      backfill: [{ id: "class:s:3", session_id: "s", seq: 3, version: 1, cursor: 3, zh: "補" }],
    }),
  });
  assert.equal(retry.closed, true);
  assert.equal(view.items.get("class:s:3").zh, "補");
  assert.ok(states.includes("live"));
  conn.stop();
  await conn.done;
}

await testDeferredHelloStaysLiveAndKeepsHistory();
await testSideDeferReschedulesWithoutClosingMain();
await testMainBackfillClosesTheSideSocket();
await testStopClosesTheBackfillSocket();
await testExtraHelloTimeoutReschedules();
console.log("room client backfill ok");
