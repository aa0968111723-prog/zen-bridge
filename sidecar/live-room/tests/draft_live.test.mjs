// round4 #5: draft captions are grey, replaced in place by the final with the same seq,
// never overwrite a final, and are hidden in projection.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { connectRoom, createCaptionView, liveTail } from "../app/static/room_client.js";
import { PACKET_SAMPLES, createPacketizer, draftUrl, toPcm16 } from "../app/static/draft_capture.js";

// 1. view: draft -> final in place
const view = createCaptionView(80);
view.apply({ type: "draft", id: "r:s:3", seq: 3, session_id: "s", room_id: "r", zh: "今天我們" });
assert.equal(view.items.size, 1);
assert.equal(view.items.get("r:s:3").draft, true);
assert.equal(view.items.get("r:s:3").en, "");
view.apply({ type: "draft", id: "r:s:3", seq: 3, session_id: "s", room_id: "r", zh: "今天我們來談" });
assert.equal(view.items.get("r:s:3").zh, "今天我們來談");
view.apply({ type: "caption", id: "r:s:3", seq: 3, session_id: "s", version: 1, zh: "今天我們來談談。", en: "" });
assert.equal(view.items.size, 1, "final replaced the draft in place (same id)");
assert.equal(view.items.get("r:s:3").draft, undefined);
assert.equal(view.items.get("r:s:3").zh, "今天我們來談談。");
// a late draft for a finished seq never overwrites the final
view.apply({ type: "draft", id: "r:s:3", seq: 3, session_id: "s", room_id: "r", zh: "亂碼" });
assert.equal(view.items.get("r:s:3").zh, "今天我們來談談。");
// a deleted line cannot come back as a draft
view.apply({ type: "caption_deleted", id: "r:s:3" });
view.apply({ type: "draft", id: "r:s:3", seq: 3, session_id: "s", zh: "復活" });
assert.equal(view.items.has("r:s:3"), false);
// next seq's draft is the live tail
view.apply({ type: "caption", id: "r:s:4", seq: 4, session_id: "s", version: 1, zh: "第四段" });
view.apply({ type: "draft", id: "r:s:5", seq: 5, session_id: "s", zh: "第五" });
assert.equal(liveTail(view.items).id, "r:s:5");
// translation update after final keeps working
view.apply({ type: "caption", id: "r:s:4", seq: 4, session_id: "s", version: 2, zh: "第四段", en: "Part four" });
assert.equal(view.items.get("r:s:4").en, "Part four");

// 2. connectRoom routes drafts to onDraft (not onEvent / versions)
const sockets = [];
const drafts = [];
const finals = [];
const conn = connectRoom({
  room: "r", url: () => "ws://x/ws/listen?room_id=r",
  openSocket: (address) => { const ws = { address, readyState: 1, sent: [], send(b) { this.sent.push(b); }, close() { this.onclose?.(); } }; sockets.push(ws); return ws; },
  sleep: () => Promise.resolve(), onState() {}, onEvent: (e) => finals.push(e), onDraft: (d) => drafts.push(d),
});
await new Promise((r) => setTimeout(r, 0));
const ws = sockets[0];
ws.onopen();
ws.onmessage({ data: JSON.stringify({ type: "hello", latest_cursor: 0, history: [] }) });
ws.onmessage({ data: JSON.stringify({ type: "draft", id: "r:s:9", seq: 9, session_id: "s", zh: "草" }) });
assert.equal(drafts.length, 1);
assert.equal(drafts[0].zh, "草");
assert.equal(finals.filter((e) => e.id === "r:s:9").length, 0);
ws.onmessage({ data: JSON.stringify({ type: "caption", id: "r:s:9", seq: 9, session_id: "s", version: 1, cursor: 1, zh: "草稿" }) });
assert.equal(finals.filter((e) => e.id === "r:s:9").length, 1, "final after draft still delivered");
conn.close?.();

// 3. packetizer: 100 ms packets of PCM16 at 16 kHz, resampled from 48 kHz
const sent = [];
const pk = createPacketizer((b) => sent.push(b), 48000);
pk.push(new Float32Array(4800 * 2).fill(0.5));    // 200 ms @ 48k
assert.equal(sent.length, 2);
assert.equal(sent[0].byteLength, PACKET_SAMPLES * 2);
const pcm = toPcm16(new Float32Array([1, -1, 0, 2]), 16000);
assert.deepEqual([...pcm], [32767, -32768, 0, 32767]);
assert.equal(draftUrl({ protocol: "https:", host: "a:1" }, "r 1", "s"), "wss://a:1/ws/draft?room_id=r%201&session_id=s");

// 4. room.html: drafts grey + hidden in projection, history uses finals only
const html = readFileSync(new URL("../app/static/room.html", import.meta.url), "utf8");
assert.match(html, /\.zh\.draft[^{]*\{[^}]*color: var\(--muted\)/);
assert.match(html, /body\.project \.draft[^{]*\{[^}]*display: none/);
assert.match(html, /orderedCaptions\(finalsOnly\(\)\)/);
assert.match(html, /onDraft: \(item\) => \{\r?\n\s+if \(privacyPaused\) return;/);
// drafts are written with textContent only (XSS)
assert.equal(/innerHTML/.test(html), false);
console.log("draft_live ok");
