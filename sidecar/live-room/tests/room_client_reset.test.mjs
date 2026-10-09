// J3. captions_cleared is the delete notification (B-d2).
// This branch calls onClear and forgets versions so the same id can be shown again.
// It does not rewind the client cursor: the server cursor counter stays put, and a
// listener reconnects with the pre-delete cursor (B-d5). onReset is the rewind used
// when latest_cursor goes backwards (J1), not the delete path.

import assert from "node:assert/strict";
import { connectRoom, createCaptionView } from "../app/static/room_client.js";

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
const cleared = [];
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
  onClear: (item) => cleared.push(item),
  onReset: () => resets.push("reset"),
});

await tick();
sockets[0].onopen();
sockets[0].onmessage({
  data: JSON.stringify({
    type: "hello",
    latest_cursor: 2,
    history: [
      { id: "class:s:1", session_id: "s", session_ord: 1, seq: 1, version: 1, cursor: 1, zh: "舊" },
      { id: "class:s:2", session_id: "s", session_ord: 1, seq: 2, version: 1, cursor: 2, zh: "也舊" },
    ],
  }),
});
assert.equal(events.length, 2);
assert.equal(conn.cursor, 2);
sockets[0].onmessage({
  data: JSON.stringify({ type: "captions_cleared", room_id: "class", epoch: 4, cursor: 3 }),
});
assert.equal(cleared.length, 1);
assert.equal(cleared[0].type, "captions_cleared");
assert.equal(resets.length, 0);
assert.equal(conn.cursor, 3, "delete does not rewind the cursor on this branch");
sockets[0].onmessage({
  data: JSON.stringify({
    id: "class:s:1",
    session_id: "s",
    session_ord: 1,
    seq: 1,
    version: 1,
    cursor: 4,
    zh: "新的第一句",
  }),
});
assert.equal(events.filter((item) => item.id === "class:s:1").length, 2);
assert.equal(events.at(-1).zh, "新的第一句");

const view = createCaptionView();
view.apply({ id: "class:s:1", session_id: "s", seq: 1, version: 1, zh: "留下" });
view.apply({ id: "class:s:2", session_id: "s", seq: 2, version: 1, zh: "也留下" });
view.apply({ type: "captions_cleared", epoch: 4, cursor: 3 });
assert.equal(view.items.size, 0);
// Tombstones keep a deleted id from coming back. A new id after the clear is shown.
view.apply({ id: "class:s:1", session_id: "s", seq: 1, version: 2, zh: "舊 id 不復活" });
assert.equal(view.items.has("class:s:1"), false);
view.apply({ id: "class:new:1", session_id: "new", seq: 1, version: 1, zh: "新的第一句" });
assert.equal(view.items.get("class:new:1").zh, "新的第一句");

conn.stop();
await conn.done;
console.log("room client reset ok");
