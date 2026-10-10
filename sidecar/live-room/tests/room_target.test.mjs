// round4 #7 T8-T10: ja target on the audience page - lang, labels, <wbr> segments, first-only ruby, XSS.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRubyMemory, modeLabels, paintTarget, targetLang } from "../app/static/room_target.js";

class Node {
  constructor(tag, text) { this.tag = tag; this.text = text; this.children = []; this.lang = ""; }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children.splice(this.children.indexOf(c), 1); }
  get firstChild() { return this.children[0] || null; }
  get textContent() { return this.text != null ? this.text : this.children.map((c) => c.textContent).join(""); }
}
const doc = { createTextNode: (t) => new Node("#text", t), createElement: (t) => new Node(t) };
const tags = (el) => el.children.map((c) => c.tag);
const all = (el, tag) => el.children.flatMap((c) => (c.tag === tag ? [c] : all(c, tag)));

assert.equal(targetLang({ tgt_lang: "ja" }), "ja");
assert.equal(targetLang({}), "en");
assert.equal(targetLang({ tgt_lang: "<x>" }), "en");
assert.equal(/英/.test(Object.values(modeLabels("ja")).join("")), false, "ja menu has no 英");
assert.equal(modeLabels("en").both, "中英");

// en: plain text
const el = new Node("div");
paintTarget(doc, el, { id: "a", en: "Hello", session_id: "s" });
assert.equal(el.lang, "en");
assert.deepEqual(tags(el), ["#text"]);

// ja: segments with <wbr>, ruby only on first line that has the term
const mem = createRubyMemory();
const first = { id: "r:s:1", session_id: "s", tgt_lang: "ja", en: "今日は、般若の意味について。",
  segments: ["今日は、", "般若の", "意味について。"], ruby: [{ text: "般若", reading: "はんにゃ" }] };
paintTarget(doc, el, first, { memory: mem });
assert.equal(el.lang, "ja");
assert.equal(el.textContent, "今日は、般若はんにゃの意味について。");
assert.equal(all(el, "wbr").length, 2);
assert.equal(all(el, "ruby").length, 1);
assert.equal(all(el, "rt")[0].textContent, "はんにゃ");
// repaint of the same line keeps its ruby
paintTarget(doc, el, first, { memory: mem });
assert.equal(all(el, "ruby").length, 1);
// a later line with the same term gets none
paintTarget(doc, el, { ...first, id: "r:s:2", en: "般若とは何か。", segments: ["般若とは", "何か。"] }, { memory: mem });
assert.equal(all(el, "ruby").length, 0);
// toggle off
paintTarget(doc, el, first, { memory: createRubyMemory(), rubyOn: false });
assert.equal(all(el, "ruby").length, 0);
// segments that do not rebuild the text are ignored (no text loss)
paintTarget(doc, el, { ...first, segments: ["偽"] }, { memory: createRubyMemory() });
assert.equal(el.textContent.replace("はんにゃ", ""), first.en);

// XSS: markup in the payload is only ever text
const evil = { id: "x", session_id: "s", tgt_lang: "ja", en: "<script>alert(1)</script><img src=x onerror=1>",
  segments: ["<script>alert(1)</script>", "<img src=x onerror=1>"], ruby: [{ text: "<b>", reading: "<i>x</i>" }] };
paintTarget(doc, el, evil, { memory: createRubyMemory() });
assert.equal(all(el, "script").length, 0);
assert.equal(all(el, "img").length, 0);
assert.ok(el.textContent.includes("<script>alert(1)</script>"));
const src = readFileSync(new URL("../app/static/room_target.js", import.meta.url), "utf8");
assert.equal(/innerHTML|insertAdjacentHTML|outerHTML/.test(src), false);

// room.html wiring
const html = readFileSync(new URL("../app/static/room.html", import.meta.url), "utf8");
assert.match(html, /#en:lang\(ja\)[^{]*\{[^}]*Yu Gothic UI[^}]*word-break: keep-all/);
assert.match(html, /id="ruby"/);
assert.match(html, /paintTarget\(document, en, item/);
assert.match(html, /class="en tgt" id="en"/);
console.log("room_target ok");
