// J1. When the server cursor moves backwards the client drops its cursor and
// asks for a full replay. The history on that replay connection is what onEvent sees.

import assert from "node:assert/strict";
import { connectRoom } from "../app/static/room_client.js";

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

const sockets = [];
const events = [];
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
  onReset: () => resets.push("reset"),
});

await tick();
sockets[0].onopen();
sockets[0].onmessage({
  data: JSON.stringify({ type: "hello", latest_cursor: 120, history: [], events: [] }),
});
assert.equal(conn.cursor, 120);
sockets[0].onclose();
await tick();
assert.match(sockets[1].address, /cursor=120/);
sockets[1].onopen();
sockets[1].onmessage({
  data: JSON.stringify({ type: "hello", latest_cursor: 3, history: [], events: [] }),
});
await tick();
// The backwards hello has no captions yet. Do not clear before they arrive.
assert.deepEqual(resets, []);
const replay = sockets.at(-1);
assert.match(replay.address, /cursor=0/);
assert.match(replay.address, /replay=1/);
const history = [
  { id: "class:s:1", session_id: "s", session_ord: 1, seq: 1, version: 1, cursor: 1, zh: "一" },
  { id: "class:s:2", session_id: "s", session_ord: 1, seq: 2, version: 1, cursor: 2, zh: "二" },
  { id: "class:s:3", session_id: "s", session_ord: 1, seq: 3, version: 1, cursor: 3, zh: "三" },
];
replay.onopen();
replay.onmessage({
  data: JSON.stringify({ type: "hello", latest_cursor: 3, history }),
});
assert.ok(resets.includes("reset"));
assert.deepEqual(events.map((item) => item.id), ["class:s:1", "class:s:2", "class:s:3"]);
assert.equal(conn.cursor, 3);
conn.stop();
await conn.done;
console.log("room client restart ok");
