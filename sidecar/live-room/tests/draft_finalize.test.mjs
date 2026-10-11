// grok/fast-draft-asr: the host's draft stream asks the server to finalise sentences, tracks the
// server's seqs, flushes the last sentence on stop and falls back to 6 s uploads when it closes.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { draftHello, handleControl, startDraftStream } from "../app/static/draft_capture.js";

assert.deepEqual(JSON.parse(draftHello("t")), { token: "t" }, "legacy hello unchanged");
assert.deepEqual(JSON.parse(draftHello("t", { finalize: true, t0Ms: 1234.6 })), { token: "t", finalize: true, t0_ms: 1235 });
const seen = [];
assert.equal(handleControl({ type: "final_seq", seq: 4 }, { onFinalSeq: (m) => seen.push(m.seq) }), true);
assert.equal(handleControl({ type: "final_dropped", seq: 5 }, { onFinalSeq: (m) => seen.push(m.seq) }), true);
assert.equal(handleControl({ type: "draft_on", seq: 1 }), false);
assert.deepEqual(seen, [4, 5]);

// fake socket + audio graph
function fakes() {
  const sockets = [];
  class WS {
    constructor(url) { this.url = url; this.readyState = 1; this.sent = []; sockets.push(this); queueMicrotask(() => this.onopen?.()); }
    send(x) { this.sent.push(x); if (typeof x === "string" && x.includes('"flush"')) queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ type: "flushed", seq: 2 }) })); }
    close() { this.readyState = 3; this.closed = true; }
  }
  class Ctx {
    constructor() { this.sampleRate = 16000; this.audioWorklet = { addModule: async () => {} }; }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    close() {}
  }
  globalThis.AudioWorkletNode = class { constructor() { this.port = {}; } disconnect() {} };
  globalThis.URL.createObjectURL = () => "blob:x";
  globalThis.URL.revokeObjectURL = () => {};
  const stream = { clone: () => ({ getTracks: () => [{ stop() {} }] }) };
  return { sockets, WS, Ctx, stream };
}

{
  const { sockets, WS, Ctx, stream } = fakes();
  const states = [];
  const floors = [];
  const handle = await startDraftStream({ stream, roomId: "r", sessionId: "s", token: "tok", loc: { protocol: "http:", host: "h" },
    AudioContextCtor: Ctx, WebSocketCtor: WS, finalize: true, t0Ms: 900,
    onFinalize: (on) => states.push(on), onFinalSeq: (m) => floors.push(m.seq), flushWaitMs: 200 });
  await new Promise((r) => setTimeout(r, 0));
  const ws = sockets[0];
  assert.deepEqual(JSON.parse(ws.sent[0]), { token: "tok", finalize: true, t0_ms: 900 });
  await ws.onmessage({ data: JSON.stringify({ type: "draft_on", seq: 1, finalize: true }) });
  assert.deepEqual(states, [true], "server agreed: host stops 6 s uploads");
  await ws.onmessage({ data: JSON.stringify({ type: "final_seq", seq: 1 }) });
  assert.deepEqual(floors, [1]);
  const t = Date.now();
  await handle.stop();
  assert.ok(ws.sent.some((x) => typeof x === "string" && x.includes('"flush"')), "stop flushes the open sentence");
  assert.ok(Date.now() - t < 200, "flushed ack ends the wait early");
  assert.equal(ws.closed, true);
  assert.deepEqual(states, [true, false]);
}

{
  // server without BREEZE_ENDPOINT_FINAL: no finalize in draft_on -> host keeps uploading
  const { sockets, WS, Ctx, stream } = fakes();
  const states = [];
  await startDraftStream({ stream, roomId: "r", sessionId: "s", token: "tok", loc: { protocol: "http:", host: "h" },
    AudioContextCtor: Ctx, WebSocketCtor: WS, finalize: true, onFinalize: (on) => states.push(on) });
  await new Promise((r) => setTimeout(r, 0));
  await sockets[0].onmessage({ data: JSON.stringify({ type: "draft_on", seq: 1 }) });
  assert.deepEqual(states, []);
}

{
  // the socket dies mid-session: finalize turns off at once (fallback to 6 s slices)
  const { sockets, WS, Ctx, stream } = fakes();
  const states = [];
  await startDraftStream({ stream, roomId: "r", sessionId: "s", token: "tok", loc: { protocol: "http:", host: "h" },
    AudioContextCtor: Ctx, WebSocketCtor: WS, finalize: true, onFinalize: (on) => states.push(on) });
  await new Promise((r) => setTimeout(r, 0));
  await sockets[0].onmessage({ data: JSON.stringify({ type: "draft_on", seq: 1, finalize: true }) });
  sockets[0].readyState = 3;
  sockets[0].onclose();
  assert.deepEqual(states, [true, false]);
}

// host page wiring (static check: the page is not importable in node)
const host = readFileSync(new URL("../app/static/host.html", import.meta.url), "utf8");
assert.match(host, /if \(endpointFinal\) return \{ ok: true, status: "skipped" \}/);
assert.match(host, /meta\.seq <= seqFloor/);
assert.match(host, /last_seq: Math\.max\(ctl\.lastSeq \|\| 0, seqFloor\)/);
assert.match(host, /await draftClosing;/);
assert.match(host, /periodMs: 6000/, "fallback slices unchanged");
console.log("draft_finalize ok");
