import assert from "node:assert/strict";
import {
  CAPTION_IDLE_MS,
  GAP_TOAST_MS,
  HISTORY_DIVIDER,
  EXPIRY_NOTE,
  contrast,
  expiryNotice,
  gapCopy,
  historyStick,
  stageMode,
  stageNote,
  stageNoteFor,
} from "../app/static/room_view.js";

assert.equal(CAPTION_IDLE_MS, 20000);
assert.equal(GAP_TOAST_MS, 6000);
assert.equal(HISTORY_DIVIDER, "— 這裡漏了一小段 —");
assert.ok(contrast("#6b5644", "#fbf6ee") >= 4.5);
assert.ok(contrast("#d9d0c3", "#16130f") >= 4.5);
assert.ok(contrast("#2a2118", "#fbf6ee") >= 4.5);
assert.ok(contrast("#16130f", "#c9843f") >= 4.5);
assert.equal(stageMode({ kind: "live", ageMs: 20000, hasCaption: true }), "live");
assert.equal(stageMode({ kind: "live", ageMs: 20001, hasCaption: true }), "idle");
assert.equal(stageMode({ kind: "live", ageMs: 60000, hasCaption: false }), "live");
assert.equal(stageMode({ kind: "reconnecting", ageMs: 0, hasCaption: true }), "stale");
assert.equal(stageMode({ kind: "device_offline", ageMs: 0, hasCaption: true }), "stale");
assert.equal(stageMode({ kind: "ended", ageMs: 0, hasCaption: true }), "ended");
assert.equal(stageNote("idle", true), "目前沒有新字幕");
assert.equal(stageNote("stale", true), "連線中斷，這是最後收到的字幕");
assert.equal(stageNote("ended", false), "這堂課已結束");
assert.equal(stageNote("empty", false), "等待主持人開始");
assert.equal(stageNote("empty", true), "等待主持人開始說話");
assert.equal(stageNoteFor("waiting_room", "empty", false), "");
assert.equal(stageNoteFor("waiting_room", "empty", true), "");
assert.equal(stageNoteFor("device_offline", "stale", false), "");
assert.equal(stageNoteFor("live", "empty", false), "等待主持人開始");
assert.equal(stageNoteFor("ended", "ended", false), "這堂課已結束");
assert.equal(gapCopy(false), "漏了一小段，已接回最新內容");
assert.equal(gapCopy(true), "已重新整理字幕");
const onScreen = new Map([["class:s2:1", { id: "class:s2:1", zh: "甲" }]]);
assert.equal(expiryNotice(["class:s2:1"], onScreen), EXPIRY_NOTE);
assert.equal(expiryNotice(["class:s2:2", "class:s2:1"], onScreen), EXPIRY_NOTE);
assert.equal(expiryNotice(["class:s1:1"], onScreen), "");
assert.equal(expiryNotice(["class:s1:1"], new Map()), "");
assert.equal(expiryNotice([], onScreen), "");
assert.equal(expiryNotice(null, onScreen), "");
assert.deepEqual(historyStick(0, 400, 100, true), { stick: true, restore: 0 });
assert.equal(historyStick(10, 400, 100, false).stick, false);
assert.equal(historyStick(260, 400, 100, false).stick, true);
console.log("room view ok");
