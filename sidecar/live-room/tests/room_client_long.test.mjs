// J4: eight failed opens, then the offline state. J5: 1000 captions × 2 versions stay ordered.

import assert from "node:assert/strict";
import { connectRoom, liveTail, orderedCaptions } from "../app/static/room_client.js";

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

async function testGivesUpAfterEight() {
  const states = [];
  let opens = 0;
  const conn = connectRoom({
    room: "class",
    url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
    openSocket() {
      opens += 1;
      throw new Error("offline");
    },
    sleep: () => Promise.resolve(),
    onState: (text) => states.push(text),
    onEvent: () => {},
  });
  await conn.done;
  assert.equal(opens, 8);
  assert.ok(states.includes("服務離線。可按重新連線。"), states.join(" | "));
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

await testGivesUpAfterEight();
await testThousandCaptionsStayOrdered();
console.log("room client long ok");
