// J2. room_unavailable reason "ended" tells the listener the room is over and
// does not burn the 8 reconnect attempts. unknown_or_ended still retries.

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
  onState: (text) => states.push(text),
  onEvent: () => {},
});

await tick();
assert.equal(sockets.length, 1);
sockets[0].onopen();
sockets[0].onmessage({
  data: JSON.stringify({ type: "room_unavailable", reason: "ended" }),
});
await conn.done;
assert.ok(states.some((text) => String(text).includes("房間已結束")), states.join(" | "));
assert.equal(sockets.length, 1);
assert.equal(sleeps, 0);
assert.equal(sockets[0].closed, true);
console.log("room client room end ok");
