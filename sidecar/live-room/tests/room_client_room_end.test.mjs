// J2. room_unavailable reason "ended" tells the listener the room is over and
// does not reconnect by itself. unknown_or_ended still retries (see the state suite).

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
const states = [];
let sleeps = 0;
const conn = connectRoom({
  room: "class",
  url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
  openSocket(address) {
    const ws = fakeSocket(address);
    sockets.push(ws);
    return ws;
  },
  sleep() {
    sleeps += 1;
    return Promise.resolve();
  },
  onState: (detail) => states.push(detail),
  onEvent: () => {},
});

await tick();
assert.equal(sockets.length, 1);
sockets[0].onopen();
sockets[0].onmessage({
  data: JSON.stringify({ type: "room_unavailable", reason: "ended" }),
});
await tick();
const rendered = states.map((detail) => (detail && detail.text) || String(detail)).join(" | ");
assert.ok(rendered.includes("已結束"), rendered);
assert.equal(rendered.includes("房間已結束"), false, rendered);
assert.ok(states.some((detail) => detail && detail.kind === "ended"), rendered);
assert.equal(sockets.length, 1);
assert.equal(sleeps, 0);
assert.equal(sockets[0].closed, true);
conn.stop();
await conn.done;
console.log("room client room end ok");
