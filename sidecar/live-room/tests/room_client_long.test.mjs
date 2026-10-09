// J4: failed opens keep retrying past eight, with jitter, and never freeze on 服務離線.
// J5: 1000 captions × 2 versions stay ordered.

import assert from "node:assert/strict";
import { connectRoom, liveTail, orderedCaptions, retryCountdown } from "../app/static/room_client.js";

function tick() {
  return new Promise((resolve) => setImmediate(resolve));
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

async function testKeepsRetryingPastEight() {
  const states = [];
  const waits = [];
  let opens = 0;
  let conn;
  conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    openSocket() {
      opens += 1;
      throw new Error("offline");
    },
    sleep(ms) {
      waits.push(ms);
      if (opens >= 10) conn.stop();
      return Promise.resolve();
    },
    random: () => 0,
    onState: (detail) => states.push(detail),
    onEvent: () => {},
  });
  await conn.done;
  assert.ok(opens > 8, `opens=${opens}`);
  assert.equal(opens, 10);
  const joined = states.map((detail) => (detail && detail.text) || "").join(" | ");
  assert.equal(joined.includes("服務離線"), false, joined);
  assert.ok(states.some((detail) => detail && detail.kind === "unreachable"), joined);
  assert.ok(states.some((detail) => detail && String(detail.text).includes("暫時連不上")), joined);
  assert.ok(states.some((detail) => detail && String(detail.subtitle).includes("下次自動重試")), joined);
  assert.ok(states.some((detail) => detail && String(detail.subtitle).includes("30 秒")), joined);
  assert.ok(
    states.some((detail) => detail && detail.kind === "unreachable" && detail.subtitle === "正在重試…"),
    states.map((detail) => detail && detail.subtitle).join(" | "),
  );
  assert.equal(retryCountdown(45000, 0), "下次自動重試：45 秒");
  assert.equal(retryCountdown(45000, 15000), "下次自動重試：30 秒");
  assert.equal(retryCountdown(45000, 44000), "下次自動重試：1 秒");
  assert.equal(retryCountdown(45000, 45000), "下次自動重試");
  assert.equal(retryCountdown(45000, 46000), "下次自動重試");
  assert.equal(states.some((detail) => detail && String(detail.subtitle) === "再試一次"), false);
  assert.deepEqual(waits, [500, 1000, 2000, 4000, 8000, 30000, 30000, 30000, 30000, 30000]);
  assert.equal(waits[0], 500);
  assert.equal(waits[5], 30000);
}

async function testThousandCaptionsStayOrdered() {
  const items = new Map();
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
    onEvent(item) { items.set(item.id, item); },
  });
  await tick();
  sockets[0].onopen();
  const started = Date.now();
  for (let version = 1; version <= 2; version += 1) {
    for (let seq = 1; seq <= 1000; seq += 1) {
      sockets[0].onmessage({
        data: JSON.stringify({
          id: `class:s:${seq}`,
          session_id: "s",
          session_ord: 1,
          seq,
          version,
          cursor: (version - 1) * 1000 + seq,
          zh: `第${seq}句`,
          en: version === 2 ? "en" : "",
        }),
      });
    }
  }
  const elapsed = Date.now() - started;
  const ordered = orderedCaptions(items);
  assert.equal(ordered.length, 1000);
  assert.deepEqual(ordered.map((item) => item.seq), Array.from({ length: 1000 }, (_, i) => i + 1));
  assert.equal(liveTail(items).seq, 1000);
  assert.equal(liveTail(items).en, "en");
  assert.ok(elapsed < 2000, `ordering took ${elapsed}ms`);
  conn.stop();
  await conn.done;
}

await testKeepsRetryingPastEight();
await testThousandCaptionsStayOrdered();
console.log("room client long ok");
