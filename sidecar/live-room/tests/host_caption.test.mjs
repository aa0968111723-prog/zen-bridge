// B-b6. The host page's big caption line lives in host_caption.js.
// A zh_ready push has no English yet; the later ready version fills it in.
// An older version of the same id must not wipe the English that already arrived.

import assert from "node:assert/strict";
import { applyCaption } from "../app/static/host_caption.js";

const state = { items: new Map() };
const zhReady = applyCaption(state, {
  id: "class:s:1",
  version: 1,
  seq: 1,
  session_ord: 1,
  cursor: 1,
  zh: "般若",
  en: "",
  status: "zh_ready",
});
assert.equal(zhReady.zh, "般若");
assert.equal(zhReady.en, "");
assert.equal(zhReady.label, "（尚無英譯）");

const ready = applyCaption(state, {
  id: "class:s:1",
  version: 2,
  seq: 1,
  session_ord: 1,
  cursor: 2,
  zh: "般若",
  en: "prajna",
  status: "ready",
});
assert.equal(ready.en, "prajna");
assert.equal(ready.label, "prajna");
assert.equal(ready.zh, "般若");

const stale = applyCaption(state, {
  id: "class:s:1",
  version: 1,
  seq: 1,
  session_ord: 1,
  cursor: 1,
  zh: "般若",
  en: "",
  status: "zh_ready",
});
assert.equal(stale.en, "prajna");
assert.equal(stale.label, "prajna");
assert.equal(state.items.get("class:s:1").version, 2);

const nextLine = applyCaption(state, {
  id: "class:s:2",
  version: 1,
  seq: 2,
  session_ord: 1,
  cursor: 3,
  zh: "下一句",
  en: "",
  status: "zh_ready",
});
assert.equal(nextLine.zh, "下一句");
assert.equal(nextLine.label, "（尚無英譯）");
const staleFirst = applyCaption(state, {
  id: "class:s:1",
  version: 2,
  seq: 1,
  session_ord: 1,
  cursor: 2,
  zh: "般若",
  en: "prajna",
  status: "ready",
});
assert.equal(staleFirst.item.id, "class:s:2");

const skipped = applyCaption(state, {
  id: "class:s:3",
  version: 2,
  seq: 3,
  session_ord: 1,
  cursor: 4,
  zh: "被略過",
  en: "",
  status: "translate_failed",
  translate_status: "skipped_backlog",
});
assert.equal(skipped.label, "（英譯積壓已略過，可重試）");
assert.equal(skipped.retry, true);
assert.equal(skipped.zh, "被略過");

const failed = applyCaption({ items: new Map() }, {
  id: "class:s:4",
  version: 2,
  seq: 4,
  session_ord: 1,
  cursor: 1,
  zh: "失敗",
  en: "",
  status: "translate_failed",
  translate_status: "timeout",
});
assert.equal(failed.label, "（英譯失敗，可重試）");
assert.equal(failed.retry, true);

const waiting = applyCaption({ items: new Map() }, {
  id: "class:s:5",
  version: 1,
  seq: 5,
  session_ord: 1,
  cursor: 1,
  zh: "還沒譯",
  en: "",
  status: "zh_ready",
});
assert.equal(waiting.label, "（尚無英譯）");
assert.equal(waiting.retry, false);
console.log("host caption ok");
