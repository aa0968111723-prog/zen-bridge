// Audience connection states: waiting room, invalid link, offline, watchdog, jitter.

import assert from "node:assert/strict";
import {
  connectRoom,
  fullDelayMs,
  reconnectDelayMs,
  waitingDelayMs,
} from "../app/static/room_client.js";

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

function texts(states) {
  return states.map((detail) => (detail && detail.text) || "").join(" | ");
}

assert.equal(waitingDelayMs(0, 0), 3500);
assert.equal(waitingDelayMs(0, 1), 6500);
assert.equal(waitingDelayMs(119999, 0), 3500);
assert.equal(waitingDelayMs(120000, 0), 10500);
assert.equal(waitingDelayMs(120000, 1), 19500);
assert.equal(waitingDelayMs(0, 0, true), 30000);
assert.equal(waitingDelayMs(0, 1, true), 60000);
assert.equal(waitingDelayMs(120000, 1, true), 60000);
assert.equal(fullDelayMs(0), 14000);
assert.equal(fullDelayMs(1), 26000);
assert.equal(reconnectDelayMs(1, 0, false), 500);
assert.equal(reconnectDelayMs(1, 1, false), 1000);
assert.equal(reconnectDelayMs(2, 0, false), 1000);
assert.equal(reconnectDelayMs(3, 0, false), 2000);
assert.equal(reconnectDelayMs(4, 0, false), 4000);
assert.equal(reconnectDelayMs(5, 0, false), 8000);
assert.equal(reconnectDelayMs(5, 1, false), 16000);
assert.equal(reconnectDelayMs(6, 0, false), 30000);
assert.equal(reconnectDelayMs(6, 1, false), 60000);
assert.equal(reconnectDelayMs(8, 0.5, true), 60000);
for (let failures = 1; failures <= 5; failures += 1) {
  const low = reconnectDelayMs(failures, 0, false);
  const high = reconnectDelayMs(failures, 1, false);
  assert.ok(low >= high * 0.5 && low <= high, `${failures} ${low} ${high}`);
  assert.ok(high <= 30000);
}

async function testWaitingRoomPollsWithoutEnding() {
  let now = 0;
  const waits = [];
  const states = [];
  const sockets = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    now: () => now,
    random: () => 0,
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep(ms) {
      waits.push(ms);
      now += ms;
      return Promise.resolve();
    },
    onState: (detail) => states.push(detail),
    onEvent: () => {},
  });
  await tick();
  let guard = 0;
  while (now < 130000 && guard < 80) {
    const ws = sockets.at(-1);
    assert.ok(ws, "poll opened a socket");
    ws.onopen();
    ws.onmessage({ data: JSON.stringify({ type: "room_unavailable", reason: "unknown_or_ended" }) });
    await tick();
    guard += 1;
  }
  assert.ok(waits.length > 8, `waits=${waits.length}`);
  assert.ok(waits.every((ms) => ms === 3500 || ms === 10500), waits.join(","));
  assert.ok(waits.includes(3500));
  assert.ok(waits.includes(10500));
  assert.equal(waits.filter((ms) => ms === 10500).length >= 1, true);
  const rendered = texts(states);
  assert.equal(rendered.includes("斷線"), false, rendered);
  assert.equal(rendered.includes("服務離線"), false, rendered);
  assert.equal(rendered.includes("房間已結束"), false, rendered);
  assert.ok(rendered.includes("等待主持人"), rendered);
  assert.ok(states.every((detail) => !detail || detail.kind !== "ended"), rendered);
  const live = sockets.at(-1);
  live.onopen();
  live.onmessage({
    data: JSON.stringify({ type: "hello", latest_cursor: 1, history: [], host_live: false }),
  });
  await tick();
  assert.equal(states.at(-1).kind, "live");
  assert.equal(states.at(-1).text, "即時字幕");
  conn.stop();
  await conn.done;
}

async function testLinkInvalidDoesNotRetryUntilNudge() {
  const states = [];
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
    onState: (detail) => states.push(detail),
    onEvent: () => {},
  });
  await tick();
  sockets[0].onclose({ code: 4401 });
  await tick();
  await tick();
  assert.equal(sockets.length, 1);
  assert.equal(states.at(-1).kind, "link_invalid");
  assert.equal(states.at(-1).text, "連結已失效，請重新掃描");
  assert.equal(states.at(-1).manual, true);
  conn.nudge();
  await tick();
  assert.equal(sockets.length, 2);
  conn.stop();
  await conn.done;
}

async function testRefusalMessageShowsWhenTheBrowserOnlyHas1006() {
  const states = [];
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
    onState: (detail) => states.push(detail),
    onEvent: () => {},
  });
  await tick();
  sockets[0].onmessage({ data: JSON.stringify({ type: "room_unavailable", reason: "link_invalid" }) });
  sockets[0].onclose({ code: 1006 });
  await tick();
  assert.equal(sockets.length, 1);
  assert.equal(states.at(-1).kind, "link_invalid");
  assert.equal(states.at(-1).text, "連結已失效，請重新掃描");
  conn.stop();
  await conn.done;

  const states2 = [];
  const sockets2 = [];
  const conn2 = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets2.push(ws);
      return ws;
    },
    sleep: () => Promise.resolve(),
    onState: (detail) => states2.push(detail),
    onEvent: () => {},
  });
  await tick();
  sockets2[0].onmessage({ data: JSON.stringify({ type: "room_unavailable", reason: "rejected" }) });
  sockets2[0].onclose({ code: 1006 });
  await tick();
  assert.equal(sockets2.length, 1);
  assert.equal(states2.at(-1).kind, "rejected");
  assert.ok(String(states2.at(-1).text).includes("無法開啟"));
  assert.equal(states2.at(-1).subtitle, "無法開啟這個房間，請確認網址或重新掃描 QR 碼");
  conn2.stop();
  await conn2.done;
}

async function testRejectedCloseStaysPut() {
  const states = [];
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
    onState: (detail) => states.push(detail),
    onEvent: () => {},
  });
  await tick();
  sockets[0].onclose({ code: 1008 });
  await tick();
  assert.equal(sockets.length, 1);
  assert.equal(states.at(-1).kind, "rejected");
  assert.equal(states.at(-1).text, "無法開啟");
  assert.equal(states.at(-1).subtitle, "無法開啟這個房間，請確認網址或重新掃描 QR 碼");
  conn.stop();
  await conn.done;
}

async function testFullDoesNotLookLikeADrop() {
  const states = [];
  const waits = [];
  const sockets = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    random: () => 1,
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep(ms) {
      waits.push(ms);
      return Promise.resolve();
    },
    onState: (detail) => states.push(detail),
    onEvent: () => {},
  });
  await tick();
  sockets[0].onopen();
  sockets[0].onmessage({ data: JSON.stringify({ type: "room_unavailable", reason: "full", retry_after_ms: 20000 }) });
  await tick();
  const full = states.filter((detail) => detail && detail.kind === "room_full");
  assert.ok(full.length >= 1);
  assert.equal(waits[0], fullDelayMs(1));
  assert.equal(full[0].text, "已滿");
  assert.ok(String(full[0].subtitle).includes("稍後自動再試"));
  const rendered = texts(states);
  assert.equal(rendered.includes("斷線"), false, rendered);
  conn.stop();
  await conn.done;
}

async function testOfflinePausesThenOnlineReconnects() {
  const states = [];
  const sockets = [];
  const listeners = { online: new Set(), offline: new Set() };
  const network = {
    onLine: true,
    addEventListener(type, fn) { listeners[type].add(fn); },
    removeEventListener(type, fn) { listeners[type].delete(fn); },
    emit(type) {
      this.onLine = type === "online";
      for (const fn of listeners[type]) fn();
    },
  };
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    network,
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep: () => new Promise(() => {}),
    onState: (detail) => states.push(detail),
    onEvent: () => {},
  });
  await tick();
  sockets[0].onopen();
  sockets[0].onmessage({ data: JSON.stringify({ type: "hello", latest_cursor: 4, history: [] }) });
  network.emit("offline");
  await tick();
  assert.equal(sockets.length, 1);
  assert.equal(states.at(-1).kind, "device_offline");
  assert.equal(states.at(-1).text, "手機沒網路");
  assert.equal(states.at(-1).subtitle, "");
  network.emit("online");
  await tick();
  assert.equal(sockets.length, 2);
  assert.ok(texts(states).includes("網路恢復，重新連線中"));
  assert.match(sockets[1].address, /cursor=4/);
  conn.stop();
  await conn.done;
}

async function testOfflineDuringBackoffDoesNotOpen() {
  const states = [];
  const sockets = [];
  let hanging = 0;
  const listeners = { online: new Set(), offline: new Set() };
  const network = {
    onLine: true,
    addEventListener(type, fn) { listeners[type].add(fn); },
    removeEventListener(type, fn) { listeners[type].delete(fn); },
    emit(type) {
      this.onLine = type === "online";
      for (const fn of listeners[type]) fn();
    },
  };
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    network,
    random: () => 0,
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep() {
      hanging += 1;
      return new Promise(() => {});
    },
    onState: (detail) => states.push(detail),
    onEvent: () => {},
  });
  await tick();
  sockets[0].onopen();
  sockets[0].onmessage({ data: JSON.stringify({ type: "hello", latest_cursor: 9, history: [] }) });
  sockets[0].close();
  await tick();
  assert.equal(hanging, 1);
  assert.equal(sockets.length, 1);
  network.emit("offline");
  await tick();
  assert.equal(sockets.length, 1, "offline must not open another socket");
  assert.equal(states.at(-1).kind, "device_offline");
  network.emit("online");
  await tick();
  assert.equal(sockets.length, 2);
  assert.ok(texts(states).includes("網路恢復，重新連線中"));
  conn.stop();
  await conn.done;
}

async function testWatchdogClosesOnlyInForeground() {
  let now = 5000;
  let foreground = true;
  let check = null;
  const states = [];
  const sockets = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    now: () => now,
    staleMs: 35000,
    schedule(fn) {
      check = fn;
      return 1;
    },
    cancelSchedule() { check = null; },
    isForeground: () => foreground,
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep: () => Promise.resolve(),
    onState: (detail) => states.push(detail),
    onEvent: () => {},
  });
  await tick();
  sockets[0].onopen();
  now = 40000;
  check();
  assert.equal(sockets[0].closed, undefined);
  now = 40001;
  foreground = false;
  check();
  assert.equal(sockets[0].closed, undefined);
  foreground = true;
  check();
  assert.equal(sockets[0].closed, true);
  await tick();
  assert.equal(sockets.length, 2);
  assert.ok(texts(states).includes("連線不穩，重新連線中"));
  conn.stop();
  await conn.done;
  assert.equal(check, null);
}

async function testWatchdogReconnectsWithoutOnclose() {
  let now = 1000;
  let check = null;
  const sockets = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    now: () => now,
    staleMs: 35000,
    schedule(fn) {
      check = fn;
      return 7;
    },
    cancelSchedule() { check = null; },
    isForeground: () => true,
    openSocket(address) {
      const ws = {
        address,
        readyState: 1,
        sent: [],
        close() { this.closed = true; },
        send() {},
      };
      sockets.push(ws);
      return ws;
    },
    sleep: () => Promise.resolve(),
    onState: () => {},
    onEvent: () => {},
  });
  await tick();
  sockets[0].onopen();
  now = 40000;
  check();
  await tick();
  assert.equal(sockets[0].closed, true);
  assert.equal(sockets[0].onclose, null);
  assert.equal(sockets[0].onmessage, null);
  assert.equal(sockets.length, 2);
  conn.stop();
  await conn.done;
}

async function testVisibleNudgeReconnectsAStaleSocketWithoutOnclose() {
  let now = 1000;
  const sockets = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    now: () => now,
    staleMs: 35000,
    schedule() { return 1; },
    cancelSchedule() {},
    isForeground: () => true,
    openSocket(address) {
      const ws = {
        address,
        readyState: 1,
        close() { this.closed = true; },
        send() {},
      };
      sockets.push(ws);
      return ws;
    },
    sleep: () => Promise.resolve(),
    onState: () => {},
    onEvent: () => {},
  });
  await tick();
  sockets[0].onopen();
  now = 40000;
  conn.nudge();
  await tick();
  assert.equal(sockets.length, 2);
  assert.equal(sockets[0].onclose, null);
  conn.stop();
  await conn.done;
}

async function testEndedRestartKeepsCursor() {
  const sockets = [];
  const states = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep: () => new Promise(() => {}),
    onState: (detail) => states.push(detail),
    onEvent: () => {},
  });
  await tick();
  sockets[0].onopen();
  sockets[0].onmessage({ data: JSON.stringify({ type: "hello", latest_cursor: 40, history: [] }) });
  sockets[0].onmessage({ data: JSON.stringify({ type: "room_unavailable", reason: "ended" }) });
  await tick();
  assert.equal(sockets.length, 1);
  assert.equal(states.at(-1).kind, "ended");
  assert.equal(conn.cursor, 40);
  conn.restart();
  await tick();
  assert.equal(sockets.length, 2);
  assert.match(sockets[1].address, /cursor=40/);
  assert.doesNotMatch(sockets[1].address, /cursor=0/);
  conn.stop();
  await conn.done;
}

async function testEndedTabReturnDoesNotReconnect() {
  const sockets = [];
  const states = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    openSocket(address) {
      const ws = fakeSocket(address);
      sockets.push(ws);
      return ws;
    },
    sleep: () => Promise.resolve(),
    onState: (detail) => states.push(detail),
    onEvent: () => {},
  });
  await tick();
  sockets[0].onopen();
  sockets[0].onmessage({ data: JSON.stringify({ type: "hello", latest_cursor: 3, history: [] }) });
  sockets[0].onmessage({ data: JSON.stringify({ type: "room_unavailable", reason: "ended" }) });
  await tick();
  assert.equal(states.at(-1).kind, "ended");
  assert.equal(sockets.length, 1);
  conn.nudge();
  await tick();
  await tick();
  assert.equal(sockets.length, 1);
  assert.equal(states.at(-1).kind, "ended");
  conn.restart();
  await tick();
  assert.equal(sockets.length, 2);
  conn.stop();
  await conn.done;
}

async function testNudgeAfterStopRestarts() {
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
    onEvent: () => {},
  });
  await tick();
  conn.stop();
  await conn.done;
  assert.equal(sockets.length, 1);
  conn.nudge();
  await tick();
  assert.equal(sockets.length, 2);
  conn.stop();
}

async function testHiddenUnreachableWaitsSixtySeconds() {
  const waits = [];
  let opens = 0;
  let conn;
  conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    random: () => 1,
    isForeground: () => false,
    openSocket() {
      opens += 1;
      throw new Error("down");
    },
    sleep(ms) {
      waits.push(ms);
      if (opens >= 6) conn.stop();
      return Promise.resolve();
    },
    onState: () => {},
    onEvent: () => {},
  });
  await conn.done;
  assert.equal(waits[5], 60000);
  assert.ok(waits[5] <= 60000);
}

async function testReconnectJitterBoundsAfterHello() {
  for (const unit of [0, 1]) {
    const waits = [];
    const sockets = [];
    const conn = connectRoom({
      room: "class",
      url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
      random: () => unit,
      openSocket(address) {
        const ws = fakeSocket(address);
        sockets.push(ws);
        return ws;
      },
      sleep(ms) {
        waits.push(ms);
        return Promise.resolve();
      },
      onState: () => {},
      onEvent: () => {},
    });
    await tick();
    sockets[0].onopen();
    sockets[0].onmessage({ data: JSON.stringify({ type: "hello", latest_cursor: 2, history: [] }) });
    sockets[0].close();
    await tick();
    const low = reconnectDelayMs(1, 0, false);
    const high = reconnectDelayMs(1, 1, false);
    assert.ok(waits.at(-1) >= low && waits.at(-1) <= high, String(waits));
    assert.equal(waits.at(-1), reconnectDelayMs(1, unit, false));
    conn.stop();
    await conn.done;
  }
}

await testWaitingRoomPollsWithoutEnding();
await testLinkInvalidDoesNotRetryUntilNudge();
await testRefusalMessageShowsWhenTheBrowserOnlyHas1006();
await testRejectedCloseStaysPut();
await testFullDoesNotLookLikeADrop();
await testOfflinePausesThenOnlineReconnects();
await testOfflineDuringBackoffDoesNotOpen();
await testWatchdogClosesOnlyInForeground();
await testWatchdogReconnectsWithoutOnclose();
await testVisibleNudgeReconnectsAStaleSocketWithoutOnclose();
await testEndedRestartKeepsCursor();
await testEndedTabReturnDoesNotReconnect();
await testNudgeAfterStopRestarts();
await testHiddenUnreachableWaitsSixtySeconds();
await testReconnectJitterBoundsAfterHello();

async function testHostLiveIsNotACaption() {
  const events = [];
  const hosts = [];
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
    onEvent: (item) => events.push(item),
    onHost: (item) => hosts.push(item),
  });
  await tick();
  sockets[0].onopen();
  sockets[0].onmessage({
    data: JSON.stringify({ type: "hello", latest_cursor: 1, history: [], host_live: true, room_id: "class" }),
  });
  sockets[0].onmessage({ data: JSON.stringify({ type: "room", room_id: "class", live: false }) });
  assert.equal(hosts.length, 2);
  assert.equal(hosts[0].live, true);
  assert.equal(hosts[1].live, false);
  assert.equal(events.length, 0);
  conn.stop();
  await conn.done;
}
await testHostLiveIsNotACaption();

async function testNewSocketDoesNotInheritThePreviousQuietClock() {
  let now = 1000;
  let check = null;
  const sockets = [];
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    now: () => now,
    staleMs: 35000,
    schedule(fn) {
      check = fn;
      return 9;
    },
    cancelSchedule() { check = null; },
    isForeground: () => true,
    openSocket(address) {
      const ws = {
        address,
        readyState: 0,
        sent: [],
        close() { this.closed = true; },
        send() {},
      };
      sockets.push(ws);
      return ws;
    },
    sleep: () => Promise.resolve(),
    onState: () => {},
    onEvent: () => {},
  });
  await tick();
  sockets[0].onopen();
  now = 40000;
  check();
  await tick();
  assert.equal(sockets.length, 2);
  assert.equal(sockets[0].closed, true);
  check();
  assert.equal(sockets[1].closed, undefined);
  now = 75000;
  check();
  assert.equal(sockets[1].closed, undefined, "35s exactly is still inside the quiet window");
  now = 75001;
  check();
  assert.equal(sockets[1].closed, true);
  conn.stop();
  await conn.done;
}
await testNewSocketDoesNotInheritThePreviousQuietClock();
console.log("room client state ok");
