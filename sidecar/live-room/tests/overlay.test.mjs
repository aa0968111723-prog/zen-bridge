// node --test tests/overlay.test.mjs  (also runnable as: node tests/overlay.test.mjs)
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { readFileSync } from "node:fs";

const here = dirname(fileURLToPath(import.meta.url));
const mod = await import(new URL("../app/static/overlay.js", import.meta.url));
const {
  DEFAULTS, MAX_LINES, FONT_STACKS, parseParams, roomFromPath, clampNumber, normalizeColor,
  fontFor, fontPx, textShadow, createLineBuffer, lineText, backoffMs, listenUrl, isCaption,
} = mod;

const cap = (id, seq, en, extra = {}) => ({ type: "caption", id, seq, session_ord: 1, version: 1, zh: "中" + seq, en, ...extra });

test("defaults when no params", () => {
  const p = parseParams("");
  assert.equal(p.room, "class");
  assert.equal(p.lang, "en");
  assert.equal(p.lines, 2);
  assert.equal(p.size, 48);
  assert.equal(p.scale, 1);
  assert.equal(p.pos, "bottom");
  assert.equal(p.color, "#ffffff");
  assert.equal(p.k, "");
  assert.equal(MAX_LINES, 2);
  assert.deepEqual(Object.keys(p).sort(), Object.keys(DEFAULTS).sort());
});

test("valid params are taken", () => {
  const p = parseParams("?room=hall-2&k=abc_DEF-1&lang=JA&show=both&lines=1&size=64&scale=1.5&pos=top&align=left&color=%23ff0&outline=00ff00&ow=5&shadow=0&margin=10&maxw=70&hide=12");
  assert.deepEqual(
    { ...p },
    { room: "hall-2", k: "abc_DEF-1", lang: "ja", show: "both", lines: 1, size: 64, scale: 1.5, pos: "top", align: "left",
      color: "#ffff00", outline: "#00ff00", ow: 5, shadow: 0, margin: 10, maxw: 70, hide: 12 },
  );
});

test("invalid params clamp or fall back", () => {
  const p = parseParams("?room=<script>&k=a%20b&lang=fr&show=x&lines=9&size=9999&scale=-3&pos=left&align=justify&color=red&outline=%23zzz&ow=NaN&shadow=7&margin=-5&maxw=5&hide=99999");
  assert.equal(p.room, "class");
  assert.equal(p.k, "");
  assert.equal(p.lang, "en");
  assert.equal(p.show, "tgt");
  assert.equal(p.lines, 2, "lines is capped at 2");
  assert.equal(p.size, 160);
  assert.equal(p.scale, 0.25);
  assert.equal(p.pos, "bottom");
  assert.equal(p.align, "center");
  assert.equal(p.color, "#ffffff");
  assert.equal(p.outline, "#000000");
  assert.equal(p.ow, 3);
  assert.equal(p.shadow, 1);
  assert.equal(p.margin, 0);
  assert.equal(p.maxw, 30);
  assert.equal(p.hide, 600);
  assert.equal(parseParams("?lines=0").lines, 1);
  assert.equal(parseParams("?lines=1.6").lines, 2);
  assert.equal(parseParams("?size=").size, 48);
  assert.equal(parseParams("?size=Infinity").size, 48);
});

test("params accept URLSearchParams and objects; path room wins", () => {
  assert.equal(parseParams(new URLSearchParams("lang=ja")).lang, "ja");
  assert.equal(parseParams({ size: "30" }).size, 30);
  assert.equal(parseParams("?room=a", "b").room, "b");
  assert.equal(roomFromPath("/overlay/room_1"), "room_1");
  assert.equal(roomFromPath("/overlay/room_1/"), "room_1");
  assert.equal(roomFromPath("/overlay"), "");
  assert.equal(roomFromPath("/overlay/%E4%B8%AD"), "");
  assert.equal(roomFromPath("/overlay/%zz"), "");
});

test("number and color helpers", () => {
  assert.equal(clampNumber("5", 0, 3, 1), 3);
  assert.equal(clampNumber(null, 0, 3, 1), 1);
  assert.equal(clampNumber("2.4", 0, 3, 1, true), 2);
  assert.equal(normalizeColor("#ABC", "#000000"), "#aabbcc");
  assert.equal(normalizeColor("javascript:1", "#000000"), "#000000");
  assert.equal(normalizeColor("#12345", "#000000"), "#000000");
});

test("font choice by language", () => {
  assert.equal(fontFor("tgt", "en").lang, "en");
  assert.equal(fontFor("tgt", "en").font, FONT_STACKS.en);
  assert.equal(fontFor("tgt", "ja").lang, "ja");
  assert.match(fontFor("tgt", "ja").font, /^"Noto Sans JP"/);
  assert.match(FONT_STACKS.ja, /Yu Gothic/);
  assert.match(FONT_STACKS.ja, /Meiryo/);
  assert.equal(fontFor("zh", "ja").lang, "zh-Hant");
  assert.match(fontFor("zh", "en").font, /Noto Sans TC/);
  assert.match(FONT_STACKS.zh, /Microsoft JhengHei/);
  for (const stack of Object.values(FONT_STACKS)) assert.match(stack, /sans-serif$/);
});

test("font size and outline", () => {
  assert.equal(fontPx({ size: 48, scale: 1 }), 48);
  assert.equal(fontPx({ size: 160, scale: 4 }), 320);
  assert.equal(fontPx({ size: 12, scale: 0.25 }), 8);
  const s = textShadow({ ow: 2, outline: "#112233", shadow: 0 });
  assert.equal(s.split(", ").length, 8);
  assert.match(s, /-2px -2px 0 #112233/);
  assert.equal(textShadow({ ow: 0, outline: "#000000", shadow: 0 }), "none");
  assert.match(textShadow({ ow: 0, outline: "#000000", shadow: 1 }), /rgba/);
});

test("buffer keeps only the latest 2 lines in order", () => {
  const b = createLineBuffer({ lines: 2 });
  for (let i = 1; i <= 5; i += 1) b.apply(cap("r:s:" + i, i, "line " + i));
  assert.deepEqual(b.visible().map((r) => r.tgt), ["line 4", "line 5"]);
  b.apply(cap("r:s:0", 0, "late old line"));
  assert.deepEqual(b.visible().map((r) => r.tgt), ["line 4", "line 5"], "out-of-order old caption stays off screen");
});

test("buffer lines=1 and hard cap at 2", () => {
  const one = createLineBuffer({ lines: 1 });
  one.apply(cap("a", 1, "x"));
  one.apply(cap("b", 2, "y"));
  assert.deepEqual(one.visible().map((r) => r.tgt), ["y"]);
  const big = createLineBuffer({ lines: 10 });
  assert.equal(big.limit, 2);
});

test("buffer updates by version and ignores stale versions", () => {
  const b = createLineBuffer();
  assert.ok(b.apply(cap("a", 1, "", { version: 1 })));
  assert.deepEqual(b.visible(), [], "untranslated caption is not shown in tgt mode");
  assert.ok(b.apply(cap("a", 1, "hello", { version: 2 })));
  assert.equal(b.apply(cap("a", 1, "old", { version: 1 })), false);
  assert.deepEqual(b.visible().map((r) => r.tgt), ["hello"]);
});

test("buffer handles delete, clear, expire and controls", () => {
  const b = createLineBuffer();
  b.apply(cap("a", 1, "A"));
  b.apply(cap("b", 2, "B"));
  b.apply(cap("c", 3, "C"));
  assert.ok(b.apply({ type: "caption_deleted", id: "c" }));
  assert.deepEqual(b.visible().map((r) => r.tgt), ["A", "B"]);
  assert.equal(b.apply(cap("c", 3, "C again", { version: 5 })), false, "deleted ids stay deleted");
  assert.ok(b.apply({ type: "captions_expired", ids: ["a"] }));
  assert.deepEqual(b.visible().map((r) => r.tgt), ["B"]);
  assert.equal(b.apply({ type: "ping", id: "x" }), false);
  assert.ok(b.apply({ type: "captions_cleared", epoch: 2 }));
  assert.deepEqual(b.visible(), []);
  b.replace([cap("x", 1, "X"), cap("y", 2, "Y"), cap("z", 3, "Z")]);
  assert.deepEqual(b.visible().map((r) => r.tgt), ["Y", "Z"]);
});

test("buffer memory stays bounded", () => {
  const b = createLineBuffer({ keep: 8 });
  for (let i = 1; i <= 500; i += 1) b.apply(cap("id" + i, i, "t" + i));
  assert.equal(b.size, 8);
  assert.deepEqual(b.visible().map((r) => r.tgt), ["t499", "t500"]);
});

test("show modes and language field", () => {
  const item = cap("a", 1, "Hello", { zh: "你好" });
  assert.deepEqual(lineText(item, "tgt", "en"), { tgt: "Hello", zh: "" });
  assert.deepEqual(lineText(item, "zh", "en"), { tgt: "", zh: "你好" });
  assert.deepEqual(lineText(item, "both", "en"), { tgt: "Hello", zh: "你好" });
  assert.equal(lineText({ ...item, en: "こんにちは" }, "tgt", "ja").tgt, "こんにちは", "ja session uses the en wire field");
  assert.equal(lineText({ ...item, ja: "やあ" }, "tgt", "ja").tgt, "やあ", "a ja field wins when present");
  assert.equal(lineText({ ...item, status: "error" }, "tgt", "en").tgt, "", "failed translation is hidden");
  const zhOnly = createLineBuffer({ show: "zh" });
  zhOnly.apply(cap("a", 1, "", { zh: "你好" }));
  assert.deepEqual(zhOnly.visible().map((r) => r.zh), ["你好"]);
});

test("isCaption", () => {
  assert.ok(isCaption({ id: "a" }));
  assert.ok(isCaption({ id: "a", type: "final" }));
  assert.equal(isCaption({ id: "a", type: "hello" }), false);
  assert.equal(isCaption({ type: "caption" }), false);
});

test("reconnect backoff grows, is capped, honors hint", () => {
  assert.equal(backoffMs(1, 1), 1000);
  assert.equal(backoffMs(1, 0), 500);
  assert.equal(backoffMs(3, 1), 4000);
  assert.equal(backoffMs(20, 1), 30000);
  assert.equal(backoffMs(100, 0), 15000);
  assert.equal(backoffMs(1, 0, 5000), 5000);
  assert.equal(backoffMs(1, 0, 10 ** 9), 60000);
  for (let n = 1; n < 30; n += 1) {
    const v = backoffMs(n, Math.random());
    assert.ok(v >= 500 && v <= 30000);
  }
});

test("listen url uses the existing audience socket", () => {
  const p = parseParams("?room=hall&k=key1");
  assert.equal(listenUrl({ protocol: "http:", host: "127.0.0.1:8765" }, p, "obs1"),
    "ws://127.0.0.1:8765/ws/listen?room_id=hall&k=key1&cid=obs1");
  assert.match(listenUrl({ protocol: "https:", host: "h" }, parseParams(""), ""), /^wss:\/\/h\/ws\/listen\?room_id=class$/);
});

test("overlay.js never uses innerHTML and loads no external resources", () => {
  const js = readFileSync(join(here, "..", "app", "static", "overlay.js"), "utf8");
  const html = readFileSync(join(here, "..", "app", "static", "overlay.html"), "utf8");
  assert.doesNotMatch(js, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(/);
  assert.doesNotMatch(html + js, /https?:\/\//);
  assert.match(html, /background: transparent/);
  assert.match(html, /overflow: hidden/);
  assert.match(html, /<script type="module" src="\/static\/overlay\.js"><\/script>/);
});
