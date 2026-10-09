// DOM wiring for the audience page: no white flash, projection keeps the
// user's text size, and dark/projection buttons clear the contrast floor.

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { pathToFileURL } from "node:url";
import vm from "node:vm";
import { contrast } from "../app/static/room_view.js";

const html = readFileSync(new URL("../app/static/room.html", import.meta.url), "utf8");
const head = html.slice(0, html.indexOf("</head>"));
const body = html.slice(html.indexOf("</head>"));

assert.equal(head.includes('<script type="module">'), false);
const inline = [...head.matchAll(/<script(?![^>]*\btype\s*=)[^>]*>([\s\S]*?)<\/script>/g)];
assert.equal(inline.length, 1, "head needs one synchronous prefs script");
const source = inline[0][1];
assert.match(source, /breeze\.audience\.prefs/);
assert.match(source, /Content-Security-Policy/);
assert.match(source, /hash/);
assert.equal(source.includes("async"), false);
assert.equal(source.includes("defer"), false);

function boot(storage, mediaDark = false) {
  const classes = [];
  const context = {
    localStorage: storage,
    document: {
      documentElement: {
        classList: { add(name) { classes.push(name); } },
      },
    },
    window: {
      matchMedia() { return { matches: !!mediaDark }; },
    },
  };
  vm.runInNewContext(source, context);
  return classes;
}

const stored = (prefs) => ({
  getItem(key) {
    return key === "breeze.audience.prefs" ? JSON.stringify(prefs) : null;
  },
});

assert.deepEqual(
  boot(stored({ mode: "project", size: "64", theme: "light" })),
  ["size-64", "project"],
);
assert.deepEqual(
  boot(stored({ mode: "both", size: "28", theme: "dark" })),
  ["size-28", "dark"],
);
assert.ok(boot(stored({ theme: "system", size: "46" }), true).includes("dark"));
assert.equal(boot(stored({ theme: "system", size: "46" }), true).includes("size-46"), true);
assert.deepEqual(boot(stored({ theme: "light" }), true), []);
assert.deepEqual(boot({ getItem() { throw new Error("SecurityError"); } }), []);
assert.deepEqual(boot({ getItem() { return "{"; } }), []);

assert.doesNotMatch(html, /body\.project\s*\{[^}]*--size\s*:/);
assert.doesNotMatch(html, /html\.project body\s*\{[^}]*--size\s*:/);
assert.match(html, /--project-scale:\s*1\.8/);
assert.match(html, /--project-vw:\s*7vw/);
assert.match(html, /--project-vh:\s*10vh/);
assert.match(html, /html\.project\.size-28, body\.project\.size-28 \{ --project-vw: 5\.5vw; --project-vh: 9vh; \}/);
assert.match(html, /html\.project\.size-46, body\.project\.size-46 \{ --project-vw: 7\.5vw; --project-vh: 8\.57vh; \}/);
assert.match(html, /html\.project\.size-64, body\.project\.size-64 \{ --project-vw: 9vw; --project-vh: 9\.444vh; \}/);
const projectFont = "font-size: max(var(--size), min(calc(var(--size) * var(--project-scale)), var(--project-vw), var(--project-vh))); overflow-wrap: anywhere; min-width: 0;";
assert.ok(html.includes("html.project .en, body.project .en { " + projectFont));
assert.ok(html.includes("html.project .zh, body.project .zh { " + projectFont));
assert.doesNotMatch(html, /project-scale\)\s*\*\s*\.62/);
assert.match(html, /html\.project body, body\.project \{[^}]*max-height:\s*100dvh[^}]*overflow:\s*hidden/);
assert.match(html, /body\.project \.stage \{[^}]*align-content:\s*flex-end[^}]*min-height:\s*0[^}]*overflow:\s*hidden/);

const buttonBody = html.slice(html.indexOf("#c9843f") - 40, html.indexOf("#c9843f") + 40);
assert.match(buttonBody, /#c9843f/);
assert.match(buttonBody, /#16130f/);
assert.ok(contrast("#16130f", "#c9843f") >= 4.5);
assert.ok(contrast("#16130f", "#c9843f") >= 6);

const header = html.split("<header>")[1].split("</header>")[0];
assert.match(header, /id="leave-project"[^>]*>離開投影</);
assert.match(html, /html\.project #leave-project, body\.project #leave-project \{ display: inline-block; \}/);
assert.match(body, /#leave-project"\)\.onclick = \(\) => \{[\s\S]*remember\(\{ \.\.\.prefs, mode: "both" \}\);[\s\S]*restoreProjectionFocus\(\)/);
assert.match(body, /leaveProjection\(prefs, ev\.key\)[\s\S]*restoreProjectionFocus\(\)/);
assert.match(body, /focusAfterProjection\(back\)/);
const style = html.slice(html.indexOf("<style>") + 7, html.indexOf("</style>"));
assert.match(style, /^\s*\[hidden\]\s*\{\s*display:\s*none\s*!important\s*;?\s*\}/);
assert.match(html, /#drawer label \{[^}]*min-height:\s*44px/);
assert.match(html, /#drawer input\[type="checkbox"\] \{ width: 24px; height: 24px; margin: 0; \}/);
assert.match(html, /id="offline"[^>]*>手機沒網路。恢復網路後會自動接回。</);
const subRule = html.match(/\.sub \{[^}]+\}/);
assert.ok(subRule);
assert.equal(subRule[0].includes("ellipsis"), false);
assert.equal(subRule[0].includes("nowrap"), false);
assert.match(html, /#state \{ flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;/);
assert.match(html, /@media \(max-width: 390px\) \{[\s\S]*html\.project #state, body\.project #state \{ flex: 1 0 100%; min-width: 100%; overflow: visible; text-overflow: unset; white-space: normal; overflow-wrap: anywhere; \}/);
assert.match(body, /document\.documentElement\.classList\.toggle/);
assert.match(body, /safeStorage\(\(\) => window\.sessionStorage\)/);
assert.match(body, /safeStorage\(\(\) => window\.localStorage\)/g);
const reads = body.match(/window\.(localStorage|sessionStorage)/g) || [];
const wrapped = body.match(/safeStorage\(\(\) => window\.(localStorage|sessionStorage)\)/g) || [];
assert.equal(reads.length, wrapped.length);
assert.ok(wrapped.length >= 3);
assert.match(body, /visibilitychange/);
assert.match(body, /conn\.nudge\(\)/);
assert.match(body, /stageNoteFor\(kind, mode, hostLive\)/);

// Stylesheet arithmetic, not a layout engine. 1rem is the browser default 16px.
// Headless Chrome on a developer machine can repeat this; CI does not launch a browser.
const REM = 16;
const ENGLISH = "A".repeat(72);
const CHINESE = "字".repeat(36);

function declaredSize(token, width) {
  const block = html.match(new RegExp(String.raw`size-${token}[^{]*\{[^}]*--size:\s*clamp\(([^)]+)\)`));
  assert.ok(block, token);
  const px = (part) => {
    const text = part.trim();
    if (text.endsWith("rem")) return parseFloat(text) * REM;
    if (text.endsWith("vw")) return (parseFloat(text) / 100) * width;
    throw new Error(text);
  };
  const [min, preferred, max] = block[1].split(",").map(px);
  return Math.min(max, Math.max(min, preferred));
}

function parseRules(sheet) {
  const rules = [];
  const chunks = [];
  let cursor = 0;
  while (cursor < sheet.length) {
    while (cursor < sheet.length && /\s/.test(sheet[cursor])) cursor += 1;
    if (cursor >= sheet.length) break;
    if (sheet.startsWith("@media", cursor)) {
      const brace = sheet.indexOf("{", cursor);
      const header = sheet.slice(cursor, brace);
      let depth = 0;
      let end = brace;
      for (; end < sheet.length; end += 1) {
        if (sheet[end] === "{") depth += 1;
        else if (sheet[end] === "}") {
          depth -= 1;
          if (depth === 0) { end += 1; break; }
        }
      }
      chunks.push({ media: header, text: sheet.slice(brace + 1, end - 1), at: brace });
      cursor = end;
      continue;
    }
    const brace = sheet.indexOf("{", cursor);
    if (brace < 0) break;
    const mediaAt = sheet.indexOf("@media", cursor);
    if (mediaAt >= 0 && mediaAt < brace) { cursor = mediaAt; continue; }
    let depth = 0;
    let end = brace;
    for (; end < sheet.length; end += 1) {
      if (sheet[end] === "{") depth += 1;
      else if (sheet[end] === "}") {
        depth -= 1;
        if (depth === 0) { end += 1; break; }
      }
    }
    chunks.push({ media: "", text: sheet.slice(cursor, end), at: cursor });
    cursor = end;
  }
  for (const chunk of chunks) {
    const pattern = /([^{}]+)\{([^{}]*)\}/g;
    let found;
    while ((found = pattern.exec(chunk.text))) {
      const decls = [];
      for (const part of found[2].split(";")) {
        const colon = part.indexOf(":");
        if (colon < 0) continue;
        const prop = part.slice(0, colon).trim().toLowerCase();
        const raw = part.slice(colon + 1).trim();
        if (!prop || !raw) continue;
        decls.push({
          prop,
          value: raw.replace(/!important/g, "").trim(),
          important: /!important/.test(raw),
        });
      }
      for (const rawSel of found[1].split(",")) {
        const selector = rawSel.trim();
        if (!selector || selector.startsWith("@")) continue;
        const order = chunk.at + found.index;
        for (const decl of decls) {
          rules.push({
            selector,
            prop: decl.prop,
            value: decl.value,
            important: decl.important,
            specificity: specificity(selector),
            order,
            media: chunk.media,
          });
        }
      }
    }
  }
  return rules;
}

function mediaMatches(header, width, height) {
  if (!header) return true;
  if (/prefers-reduced-motion|prefers-color-scheme/.test(header)) return false;
  const max = header.match(/max-width:\s*([\d.]+)px/);
  const min = header.match(/min-width:\s*([\d.]+)px/);
  const maxH = header.match(/max-height:\s*([\d.]+)px/);
  const minH = header.match(/min-height:\s*([\d.]+)px/);
  if (max && width > parseFloat(max[1])) return false;
  if (min && width < parseFloat(min[1])) return false;
  if (maxH || minH) {
    if (height == null) return false;
    if (maxH && height > parseFloat(maxH[1])) return false;
    if (minH && height < parseFloat(minH[1])) return false;
  }
  return true;
}

const cssRules = parseRules(style);

function rankBetter(next, prev) {
  for (let i = 0; i < next.length; i += 1) {
    if (next[i] === prev[i]) continue;
    return next[i] > prev[i];
  }
  return false;
}

function computedProp(el, prop, width, height) {
  let winner = null;
  for (const rule of cssRules) {
    if (rule.prop !== prop) continue;
    if (!mediaMatches(rule.media, width, height)) continue;
    if (!matchesSelector(el, rule.selector)) continue;
    const rank = [rule.important ? 1 : 0, ...rule.specificity, rule.order];
    if (!winner || rankBetter(rank, winner.rank)) winner = { value: rule.value, rank };
  }
  if (winner) return winner.value;
  if (prop.startsWith("--") && el.parent) return computedProp(el.parent, prop, width, height);
  return null;
}

function pageElement(token, project) {
  const classes = new Set(project ? ["project", "size-" + token] : ["size-" + token]);
  const root = element({ tag: "html", classes });
  const body = element({ tag: "body", classes: new Set(classes), parent: root });
  const stage = element({ tag: "div", id: "stage", classes: new Set(["stage"]), parent: body });
  const en = element({ tag: "div", id: "en", classes: new Set(["en"]), parent: stage });
  const zh = element({ tag: "div", id: "zh", classes: new Set(["zh"]), parent: stage });
  const header = element({ tag: "header", parent: body });
  const state = element({ tag: "span", id: "state", parent: header });
  const note = element({ tag: "p", id: "stage-note", classes: new Set(["stage-note"]), parent: body });
  const item = element({ tag: "li", parent: body });
  const drawer = element({ tag: "div", id: "drawer", parent: body });
  const label = element({ tag: "p", id: "hist-label", parent: body });
  const history = element({ tag: "ul", id: "history", parent: body });
  return { root, body, stage, en, zh, header, state, note, item, drawer, label, history };
}

function evalLength(value, width, height) {
  let text = String(value == null ? "" : value).trim();
  if (!text) return null;
  text = text.replace(/env\(\s*safe-area-inset-(?:top|right|bottom|left)\s*(?:,\s*[^)]*)?\)/g, "0px");
  if ((text.startsWith("min(") || text.startsWith("max(")) && text.endsWith(")")) {
    const nums = text.slice(4, -1).split(",").map((part) => evalLength(part, width, height)).filter((part) => part != null);
    if (!nums.length) return null;
    return text.startsWith("min(") ? Math.min(...nums) : Math.max(...nums);
  }
  if (text.startsWith("clamp(") && text.endsWith(")")) {
    const parts = text.slice(6, -1).split(",").map((part) => evalLength(part, width, height));
    return Math.min(parts[2], Math.max(parts[0], parts[1]));
  }
  if (text.startsWith("calc(") && text.endsWith(")") && !text.slice(5, -1).includes("(")) {
    const pieces = text.slice(5, -1).split("+").map((part) => part.trim()).filter(Boolean);
    if (!pieces.length) return null;
    let total = 0;
    for (const piece of pieces) {
      const n = evalLength(piece, width, height);
      if (n == null) return null;
      total += n;
    }
    return total;
  }
  if (text.endsWith("vw")) return (parseFloat(text) / 100) * width;
  if (text.endsWith("vh")) return (parseFloat(text) / 100) * height;
  if (text.endsWith("rem")) return parseFloat(text) * REM;
  if (text.endsWith("px")) return parseFloat(text);
  const bare = Number(text);
  return Number.isFinite(bare) ? bare : null;
}

function computedProjectFont(width, height, token) {
  const nodes = pageElement(token, true);
  const size = evalLength(computedProp(nodes.en, "--size", width), width, height);
  const scale = evalLength(computedProp(nodes.en, "--project-scale", width), width, height);
  const vw = evalLength(computedProp(nodes.en, "--project-vw", width), width, height);
  const vh = evalLength(computedProp(nodes.en, "--project-vh", width), width, height);
  const fontValue = computedProp(nodes.en, "font-size", width) || "";
  const compact = fontValue.replace(/\s+/g, "");
  const formula = "max(var(--size),min(calc(var(--size)*var(--project-scale)),var(--project-vw),var(--project-vh)))";
  if (compact === formula) return Math.max(size, Math.min(size * scale, vw, vh));
  if (compact === "calc(var(--size)*var(--project-scale))") return size * scale;
  if (compact === "var(--size)") return size;
  throw new Error("unparsed project font-size: " + fontValue);
}

function projectFontPx(sizePx, width, height, token) {
  const font = computedProjectFont(width, height, token);
  assert.ok(font > 0, sizePx);
  return font;
}

function captionContentWidth(viewport) {
  const stagePad = 16;
  const textPad = 40;
  const itemBorder = 18 * REM + textPad;
  const inner = viewport - stagePad;
  if (inner >= itemBorder * 2) return inner / 2 - textPad;
  return inner - textPad;
}

function blockHeight(fontPx, chars, contentWidth, charRatio, lineHeight, padY) {
  const perLine = Math.max(1, Math.floor(contentWidth / (fontPx * charRatio)));
  const lines = Math.ceil(chars / perLine);
  return padY + lines * fontPx * lineHeight;
}

const viewports = [
  { width: 320, height: 568 },
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1280, height: 720 },
];
function authorHiddenWins(sheet) {
  return /^\s*\[hidden\]\s*\{\s*display:\s*none\s*!important/.test(sheet);
}

// Header: 8+8 padding and a 44px control row. At <=390 the status wraps onto its own line.
function headerHeight(width) {
  const padY = 8 + 8;
  const nodes = pageElement("34", true);
  const minHeight = evalLength(computedProp(nodes.header, "min-height", width), width, 800);
  assert.ok(minHeight > 0, "header min-height");
  const row = minHeight;
  const wraps = width <= 390 && /@media \(max-width:\s*390px\)[\s\S]*#state \{ flex: 1 0 100%;/.test(html);
  if (!wraps) return padY + row;
  return padY + row + 8 + 24;
}

// Open drawer: padding 8+12, 44px rows, 8px gaps. Phones wrap to three rows
// (168px); from 768px the controls sit on one row (64px). Projection collapses it.
function openDrawerHeight(width) {
  const pad = 8 + 12;
  const row = 44;
  const gap = 8;
  const rows = width < 768 ? 3 : 1;
  return pad + rows * row + (rows - 1) * gap;
}

function drawerHeight(width) {
  if (authorHiddenWins(style)) return 0;
  return openDrawerHeight(width);
}

const phoneFonts = ["28", "34", "46", "64"].map((token) => projectFontPx(declaredSize(token, 390), 390, 844, token));
assert.ok(phoneFonts[0] < phoneFonts[1], `phone 小 ${phoneFonts[0]} vs 中 ${phoneFonts[1]}`);
const deskFonts = ["28", "34", "46", "64"].map((token) => projectFontPx(declaredSize(token, 1280), 1280, 720, token));
assert.ok(deskFonts[2] < deskFonts[3], `720p 大 ${deskFonts[2]} vs 特大 ${deskFonts[3]}`);
for (let i = 1; i < deskFonts.length; i += 1) {
  assert.ok(deskFonts[i - 1] <= deskFonts[i], deskFonts.join(","));
}

for (const viewport of viewports) {
  for (const size of ["28", "34", "46", "64"]) {
    const sizePx = declaredSize(size, viewport.width);
    const font = projectFontPx(sizePx, viewport.width, viewport.height, size);
    const content = captionContentWidth(viewport.width);
    assert.ok(font <= content, `glyph ${font}px wider than ${content}px at ${viewport.width} size-${size}`);
    const natural = [...CHINESE].length * font;
    const canWrap = html.includes("overflow-wrap: anywhere") && html.includes("min-width: 0");
    const usedWidth = canWrap ? font : natural;
    assert.ok(usedWidth <= content, `zh uses ${usedWidth}px > ${content}px at ${viewport.width} size-${size}`);
    const stacked = viewport.width - 16 < (18 * REM + 40) * 2;
    const enH = blockHeight(font, ENGLISH.length, content, 0.5, 1.35, 20);
    const zhH = blockHeight(font, [...CHINESE].length, content, 1, 1.45, 20);
    const block = stacked ? enH + zhH : Math.max(enH, zhH);
    const header = headerHeight(viewport.width);
    const drawer = drawerHeight(viewport.width);
    const bottom = header + drawer + block;
    const nodes = pageElement(size, true);
    const stageAlign = computedProp(nodes.stage, "align-content", viewport.width);
    const stageOverflow = computedProp(nodes.stage, "overflow", viewport.width);
    const clips = /max-height:\s*100dvh/.test(html) && stageOverflow === "hidden" && stageAlign === "flex-end";
    const clipOnly = viewport.width === 320 && viewport.height === 568 && size === "64";
    if (bottom > viewport.height) {
      assert.ok(clipOnly, `bottom ${bottom} = header ${header} + drawer ${drawer} + caption ${block} exceeds ${viewport.height} at ${viewport.width} size-${size}`);
      assert.ok(clips, `bottom ${bottom} = header ${header} + drawer ${drawer} + caption ${block} exceeds ${viewport.height} at ${viewport.width} size-${size}`);
      const room = viewport.height - header - drawer;
      const newest = font * 1.35 + 20;
      assert.ok(room >= newest, `room ${room}px < newest line ${newest}px at ${viewport.width} size-${size} (header ${header} drawer ${drawer})`);
    }
    if (viewport.width === 320 && viewport.height === 568 && size === "46") {
      assert.ok(bottom <= viewport.height, `320 size-46 must fit: header ${header} + drawer ${drawer} + caption ${block} = ${bottom}`);
    }
  }
}
const uncapped = declaredSize("64", 1280) * 1.8;
const uncappedHeight = blockHeight(uncapped, ENGLISH.length, captionContentWidth(1280), 0.5, 1.35, 20);
assert.ok(uncappedHeight > 720, "the fixture must still overflow the old uncapped projection size");

const fontViewports = [
  [320, 568], [360, 740], [390, 844], [430, 932],
  [768, 1024], [1280, 720], [1920, 1080], [720, 1280], [1080, 1920],
];
for (const [width, height] of fontViewports) {
  const fonts = ["28", "34", "46", "64"].map((token) => projectFontPx(declaredSize(token, width), width, height, token));
  for (let i = 1; i < fonts.length; i += 1) {
    assert.ok(fonts[i - 1] < fonts[i], `${width}x${height} ${fonts.join(",")}`);
  }
}
const fonts720 = ["28", "34", "46", "64"].map((token) => projectFontPx(declaredSize(token, 1280), 1280, 720, token));
const gap720 = (fonts720[3] - fonts720[2]) / fonts720[2];
assert.ok(gap720 >= 0.1, `720p 大→特大 ${(gap720 * 100).toFixed(1)}% of ${fonts720.join(",")}`);

// The fixture sentence is 72 capital A's beside 36 full-width characters.
// Room is the 720p stage after the header, the stage note, and stage padding.
// A wider capital (0.7em, not the optimistic 0.5) is what makes 10vh wrap off the stage.
function projectStageRoom(width, height) {
  const nodes = pageElement("64", true);
  const header = headerHeight(width);
  const note = evalLength(computedProp(nodes.note, "min-height", width, height), width, height) || 0;
  return height - header - note - (8 + 12);
}
function sentenceBox(font, width) {
  const content = captionContentWidth(width);
  const stacked = width - 16 < (18 * REM + 40) * 2;
  const enH = blockHeight(font, ENGLISH.length, content, 0.7, 1.35, 20);
  const zhH = blockHeight(font, [...CHINESE].length, content, 1, 1.45, 20);
  return stacked ? enH + zhH : Math.max(enH, zhH);
}
const font720 = fonts720[3];
const sentence720 = sentenceBox(font720, 1280);
const room720 = projectStageRoom(1280, 720);
assert.ok(sentence720 <= room720, `720p 特大 sentence ${sentence720}px is outside the ${room720}px stage at ${font720}px`);
// Chrome at 1280×720 clips this fixture once the glyph exceeds 68px
// (69.12px already paints above the stage). The model alone let 69–70px through.
assert.ok(font720 <= 68, `720p 特大 ${font720}px exceeds the measured 68px cap`);

const stageSample = pageElement("64", true);
assert.equal(computedProp(stageSample.stage, "align-content", 1280), "flex-end");
assert.equal(computedProp(stageSample.stage, "overflow", 1280), "hidden");
assert.equal(computedProp(stageSample.stage, "flex-wrap", 1280), "wrap");
assert.equal(computedProp(stageSample.stage, "flex-direction", 1280), "row");

function percentCap(value, inner) {
  if (!value) return null;
  const match = String(value).trim().match(/^([\d.]+)%$/);
  if (!match) return null;
  return (parseFloat(match[1]) / 100) * inner;
}

function newestPair(viewport, token, english, chinese, drawerOpen) {
  const nodes = pageElement(token, true);
  const width = viewport.width;
  const font = projectFontPx(declaredSize(token, width), width, viewport.height, token);
  const header = headerHeight(width);
  const drawer = drawerOpen ? openDrawerHeight(width) : 0;
  const inner = viewport.height - header - drawer - 20;
  const content = captionContentWidth(width);
  const enH = blockHeight(font, english.length, content, 0.5, 1.35, 20);
  const zhH = blockHeight(font, [...chinese].length, content, 1, 1.45, 20);
  const stacked = width - 16 < (18 * REM + 40) * 2;
  const enJustify = computedProp(nodes.en, "justify-content", width);
  const enOverflow = computedProp(nodes.en, "overflow", width);
  const cap = enOverflow === "hidden" && enJustify === "flex-end"
    ? percentCap(computedProp(nodes.en, "max-height", width), inner)
    : null;
  const enBox = cap != null ? Math.min(enH, cap) : enH;
  const zhBox = cap != null ? Math.min(zhH, cap) : zhH;
  const block = stacked ? enBox + zhBox : Math.max(enBox, zhBox);
  const direction = computedProp(nodes.stage, "flex-direction", width);
  const wrap = computedProp(nodes.stage, "flex-wrap", width);
  const align = computedProp(nodes.stage, "align-content", width);
  const justify = computedProp(nodes.stage, "justify-content", width);
  const overflow = computedProp(nodes.stage, "overflow", width);
  const packEnd = direction === "column"
    ? justify === "flex-end"
    : wrap !== "wrap-reverse" && align === "flex-end" && (overflow === "hidden" || overflow === "clip");
  const top = packEnd ? inner - block : 0;
  const enTop = top;
  const enBottom = stacked ? enTop + enBox : top + block;
  const zhTop = stacked ? enBottom : top;
  const zhBottom = stacked ? zhTop + zhBox : top + block;
  const enLine = font * 1.35;
  const zhLine = font * 1.45;
  const faded = (node) => {
    const opacity = computedProp(node, "opacity", width);
    return opacity != null && Number(opacity) === 0;
  };
  const enHidden = computedProp(nodes.en, "visibility", width) === "hidden" || computedProp(nodes.en, "display", width) === "none" || faded(nodes.en);
  const zhHidden = computedProp(nodes.zh, "visibility", width) === "hidden" || computedProp(nodes.zh, "display", width) === "none" || faded(nodes.zh);
  const inside = (lineTop, line, hidden) => !hidden && lineTop >= -0.5 && lineTop + line <= inner + 0.5;
  return {
    en: inside(enBottom - enLine, enLine, enHidden),
    zh: inside(zhBottom - zhLine, zhLine, zhHidden),
    font,
    inner,
  };
}

const LONG_EN = "y".repeat(260);
const LONG_ZH = "字".repeat(90);
for (const viewport of [
  { width: 320, height: 568, drawer: false },
  { width: 320, height: 568, drawer: true },
  { width: 568, height: 320, drawer: false },
  { width: 640, height: 360, drawer: false },
]) {
  const lines = newestPair(viewport, "64", LONG_EN, LONG_ZH, viewport.drawer);
  const where = `${viewport.width}x${viewport.height} drawer ${viewport.drawer} font ${lines.font} inner ${lines.inner}`;
  assert.ok(lines.en, "en newest off screen " + where);
  assert.ok(lines.zh, "zh newest off screen " + where);
  const nodes = pageElement("64", true);
  const wrap = computedProp(nodes.en, "overflow-wrap", viewport.width);
  assert.equal(wrap, "anywhere", where);
  const natural = LONG_EN.length * lines.font * 0.5;
  const used = wrap === "anywhere" ? Math.min(natural, captionContentWidth(viewport.width)) : natural;
  assert.ok(used <= viewport.width, `horizontal ${used} > ${viewport.width}`);
}

const noteAt = html.indexOf('id="stage-note"');
const stageAt = html.indexOf('<div class="stage"');
assert.ok(noteAt !== -1 && stageAt !== -1 && noteAt < stageAt, "stage note must sit outside the clipped stage");

function englishHiddenBy(sheet) {
  const nodes = pageElement("64", true);
  const pattern = /([^{}@]+)\{([^{}]*)\}/g;
  let found;
  while ((found = pattern.exec(sheet))) {
    const visibility = found[2].match(/(?:^|;)\s*visibility\s*:\s*([^;]+)/);
    const display = found[2].match(/(?:^|;)\s*display\s*:\s*([^;]+)/);
    const opacity = found[2].match(/(?:^|;)\s*opacity\s*:\s*([^;]+)/);
    const hides = (visibility && /hidden/.test(visibility[1])) || (display && /^\s*none\b/.test(display[1])) || (opacity && Number(opacity[1]) === 0);
    if (!hides) continue;
    for (const raw of found[1].split(",")) {
      const selector = raw.trim().replace(/:last-of-type|:last-child/g, "");
      if (selector && matchesSelector(nodes.en, selector)) return raw.trim();
    }
  }
  return "";
}
assert.equal(englishHiddenBy(style), "");

const normal = pageElement("64", false);
assert.equal(computedProp(normal.stage, "overflow", 320), "hidden");
assert.equal(computedProp(normal.stage, "justify-content", 320), "flex-end");
assert.equal(computedProp(normal.en, "overflow-wrap", 320), "anywhere");
assert.equal(computedProp(normal.zh, "overflow-wrap", 320), "anywhere");
assert.equal(computedProp(normal.item, "overflow-wrap", 320), "anywhere");
{
  const width = 320;
  const height = 568;
  const font = declaredSize("64", width);
  const header = headerHeight(width);
  const history = /max-height:\s*28vh/.test(html) ? 0.28 * height : 0;
  const inner = height - header - history - 20;
  const block = blockHeight(font, 90, captionContentWidth(width), 1, 1.45, 20);
  const justify = computedProp(normal.stage, "justify-content", width);
  const overflow = computedProp(normal.stage, "overflow", width);
  const line = font * 1.45;
  const lineTop = justify === "flex-end" && overflow === "hidden"
    ? inner - line
    : justify === "center"
      ? (inner - block) / 2 + block - line
      : block - line;
  assert.ok(lineTop >= -0.5 && lineTop + line <= inner + 0.5, `normal newest ${lineTop} inner ${inner}`);
}

function paddingEdges(el, width, height) {
  const shorthand = computedProp(el, "padding", width, height) || "";
  const parts = shorthand.split(/\s+/).filter(Boolean);
  const len = (part) => evalLength(part, width, height) || 0;
  let top = 0;
  let bottom = 0;
  if (parts.length === 1) top = bottom = len(parts[0]);
  else if (parts.length === 2) { top = len(parts[0]); bottom = top; }
  else if (parts.length >= 3) { top = len(parts[0]); bottom = len(parts[2]); }
  const longTop = computedProp(el, "padding-top", width, height);
  const longBottom = computedProp(el, "padding-bottom", width, height);
  if (longTop) top = len(longTop);
  if (longBottom) bottom = len(longBottom);
  return { top, bottom };
}

function flowHeight(el, width, height, fallback) {
  const display = computedProp(el, "display", width, height);
  if (display === "none") return 0;
  return fallback;
}

// Normal mode keeps the page locked, but each language crops from its own
// bottom so the newest English line is not the thing that disappears. A short
// landscape screen drops the history list and caps the drawer so the two
// newest lines sit clear of that overlay.
function normalNewest(viewport, token, english, chinese, drawerOpen) {
  const width = viewport.width;
  const height = viewport.height;
  const nodes = pageElement(token, false);
  const font = declaredSize(token, width);
  const enLine = font * 1.35;
  const zhLine = font * 0.62 * 1.45;
  const header = 16 + 44;
  const sub = flowHeight(nodes.note, width, height, 0) === 0 && height <= 400 ? 8 : 8;
  const note = flowHeight(nodes.note, width, height, evalLength(computedProp(nodes.note, "min-height", width, height), width, height) || 24);
  const label = flowHeight(nodes.label, width, height, 20);
  const history = flowHeight(nodes.history, width, height, 0.28 * height);
  const drawerPos = computedProp(nodes.drawer, "position", width, height);
  const drawerMax = evalLength(computedProp(nodes.drawer, "max-height", width, height), width, height);
  const naturalDrawer = openDrawerHeight(width);
  const drawerH = drawerOpen ? Math.min(naturalDrawer, drawerMax == null ? naturalDrawer : drawerMax) : 0;
  // A static drawer sits in the column and shrinks the stage. Absolute overlays instead.
  const flowDrawer = drawerOpen && drawerPos !== "absolute" && drawerPos !== "fixed" ? drawerH : 0;
  let stage = Math.max(0, height - header - sub - flowDrawer - note - label - history);
  const stageTop = header + sub + flowDrawer + note;
  const padTop = 8;
  const padBottom = 12;
  const contentTop = stageTop + padTop;
  const contentBottom = stageTop + stage - padBottom;
  const contentH = Math.max(0, contentBottom - contentTop);
  const enCap = percentCap(computedProp(nodes.en, "max-height", width, height), contentH);
  const zhCap = percentCap(computedProp(nodes.zh, "max-height", width, height), contentH);
  const enNatural = blockHeight(font, english.length, captionContentWidth(width), 0.5, 1.35, 0);
  const zhNatural = blockHeight(font * 0.62, [...chinese].length, captionContentWidth(width), 1, 1.45, 0);
  const enBox = enCap != null ? Math.min(enNatural, enCap) : enNatural;
  const zhBox = zhCap != null ? Math.min(zhNatural, zhCap) : zhNatural;
  const enPad = paddingEdges(nodes.en, width, height);
  const zhPad = paddingEdges(nodes.zh, width, height);
  const blockBottom = contentBottom;
  const enBottom = blockBottom - zhBox;
  const enTop = enBottom - enBox;
  const zhTop = enBottom;
  const zhBottom = blockBottom;
  const enClips = computedProp(nodes.en, "overflow", width, height) === "hidden"
    && computedProp(nodes.en, "justify-content", width, height) === "flex-end";
  const zhClips = computedProp(nodes.zh, "overflow", width, height) === "hidden"
    && computedProp(nodes.zh, "justify-content", width, height) === "flex-end";
  // Newest line is the last line. flex-end keeps it inside a capped box; otherwise
  // it sits at the end of the natural block and a max-height clips it off.
  const enGlyphBottom = enClips ? enBottom - enPad.bottom : enTop + enNatural;
  const enGlyphTop = enGlyphBottom - enLine;
  const zhGlyphBottom = zhClips ? zhBottom - zhPad.bottom : zhTop + zhNatural;
  const zhGlyphTop = zhGlyphBottom - zhLine;
  // An absolute drawer with no top is placed at the flex start (measured 0)
  // and covers the header. A declared top is the overlay's real edge.
  const declaredTop = evalLength(computedProp(nodes.drawer, "top", width, height), width, height);
  const drawerTop = drawerPos === "absolute" || drawerPos === "fixed"
    ? (declaredTop == null ? 0 : declaredTop)
    : header + sub;
  const drawerBottom = drawerTop + drawerH;
  const covers = (top, bottom) => drawerOpen && drawerPos === "absolute" && top < drawerBottom - 0.5 && bottom > drawerTop + 0.5;
  const shown = (top, bottom, boxTop, boxBottom) => top >= boxTop - 0.5 && bottom <= boxBottom + 0.5
    && top >= -0.5 && bottom <= height + 0.5 && bottom > top
    && !covers(top, bottom);
  return {
    en: shown(enGlyphTop, enGlyphBottom, enTop, enBottom) && enGlyphTop >= contentTop - 0.5,
    zh: shown(zhGlyphTop, zhGlyphBottom, zhTop, zhBottom) && zhGlyphBottom <= contentBottom + 0.5,
    font,
    enGlyphTop,
    zhGlyphTop,
    drawerTop,
    drawerBottom,
    contentH,
  };
}

for (const viewport of [
  { width: 320, height: 568, drawer: false },
  { width: 360, height: 740, drawer: false },
  { width: 390, height: 844, drawer: false },
  { width: 430, height: 932, drawer: false },
  { width: 568, height: 320, drawer: false },
  { width: 640, height: 360, drawer: false },
  { width: 568, height: 320, drawer: true },
  { width: 640, height: 360, drawer: true },
]) {
  for (const size of ["28", "34", "46", "64"]) {
    const lines = normalNewest(viewport, size, LONG_EN, LONG_ZH, viewport.drawer);
    const where = `normal ${viewport.width}x${viewport.height} drawer ${viewport.drawer} size-${size} font ${lines.font}`;
    assert.ok(lines.en, `en newest off screen ${where} top ${lines.enGlyphTop} drawer ${lines.drawerBottom}`);
    assert.ok(lines.zh, `zh newest off screen ${where} top ${lines.zhGlyphTop} drawer ${lines.drawerBottom}`);
  }
}

// Header content box is min-height 44px plus 8px padding on each side (60px),
// then the notch inset. #state and ⚙ 設定 sit in that band. An absolute drawer
// with no top used to start at 0 and cover both, and the only close control
// was the covered button.
function normalHeaderBottom() {
  return 8 + 44 + 8;
}
const drawerScreens = [
  { width: 320, height: 568 },
  { width: 360, height: 640 },
  { width: 390, height: 844 },
  { width: 430, height: 932 },
  { width: 568, height: 320 },
  { width: 640, height: 360 },
  { width: 844, height: 390 },
  { width: 768, height: 1024 },
  { width: 1280, height: 720 },
];
assert.match(style, /html:not\(\.project\) \.en \{ max-height: 60%; \}/);
assert.match(style, /html:not\(\.project\) \.zh \{ max-height: 40%; \}/);
assert.equal(drawerScreens.length, 9);
assert.match(header, /id="settings"/);
assert.match(header, /id="state"/);
assert.match(html, /id="drawer-close"[^>]*aria-label="關閉設定"/);
assert.match(body, /ev\.key === "Escape"[\s\S]*setDrawer\(false\)/);
assert.match(body, /#drawer-close"\)\.onclick = \(\) => setDrawer\(false\)/);
for (const viewport of drawerScreens) {
  const nodes = pageElement("34", false);
  const where = `${viewport.width}x${viewport.height}`;
  const pos = computedProp(nodes.drawer, "position", viewport.width, viewport.height);
  assert.equal(pos, "absolute", where);
  const drawerTop = evalLength(computedProp(nodes.drawer, "top", viewport.width, viewport.height), viewport.width, viewport.height);
  const headerBottom = normalHeaderBottom();
  assert.ok(drawerTop != null && drawerTop >= headerBottom - 0.5, `open drawer covers 設定 ${where} top ${drawerTop} header ${headerBottom}`);
  const headerZ = Number(computedProp(nodes.header, "z-index", viewport.width, viewport.height));
  const drawerZ = Number(computedProp(nodes.drawer, "z-index", viewport.width, viewport.height));
  assert.ok(headerZ > drawerZ, `設定 must paint above the drawer ${where} header ${headerZ} drawer ${drawerZ}`);
  const lines = normalNewest(viewport, "34", LONG_EN, LONG_ZH, true);
  assert.ok(Math.abs(lines.drawerTop - drawerTop) < 0.5, `normalNewest drawer top ${lines.drawerTop} != css ${drawerTop} ${where}`);
}

function projectionStateWidth(viewport) {
  const content = viewport - 24;
  const ownRow = viewport <= 390 && /@media \(max-width:\s*390px\)[\s\S]*#state \{ flex: 1 0 100%; min-width: 100%;/.test(html);
  if (ownRow) return content;
  return content - (24 + 4 * 16) - (24 + 3 * 16) - 16;
}
assert.ok(projectionStateWidth(320) >= 240, String(projectionStateWidth(320)));
assert.ok(projectionStateWidth(390) >= 240, String(projectionStateWidth(390)));

assert.match(body, /const text = expiryNotice\(data && data\.ids, items\);\s*if \(!text\) return;/);
assert.match(body, /retryCountdown\(connDetail\.nextRetryAt/);
assert.match(body, /setInterval\(\(\) => \{ paintChrome\(\); paintSubtitle\(\); \}, 1000\)/);

// Author display vs the user-agent [hidden] rule. #drawer { display:flex } and
// #drawer label { display:inline-flex } used to win, so hidden stayed on screen.
function identAt(text, index) {
  const match = /^[\w-]+/.exec(text.slice(index));
  return { name: match ? match[0] : "", next: index + (match ? match[0].length : 0) };
}

function matchCompound(el, compound) {
  if (compound === "*") return true;
  let index = 0;
  let saw = false;
  while (index < compound.length) {
    const ch = compound[index];
    if (ch === "#") {
      const id = identAt(compound, index + 1);
      if (el.id !== id.name) return false;
      index = id.next;
      saw = true;
    } else if (ch === ".") {
      const cls = identAt(compound, index + 1);
      if (!el.classes.has(cls.name)) return false;
      index = cls.next;
      saw = true;
    } else if (ch === "[") {
      const end = compound.indexOf("]", index);
      if (end < 0) return false;
      const inner = compound.slice(index + 1, end);
      if (inner === "hidden") {
        if (!el.hidden) return false;
      } else {
        const attr = inner.match(/^([\w-]+)(?:\s*[~|^$*]?=\s*"?([^"\]]+)"?)?$/);
        if (!attr) return false;
        const actual = el.attrs && el.attrs[attr[1]];
        if (attr[2] == null) {
          if (actual == null) return false;
        } else if (actual !== attr[2]) return false;
      }
      index = end + 1;
      saw = true;
    } else if (ch === ":") {
      if (compound.startsWith(":not(", index)) {
        let depth = 0;
        let end = index + 4;
        for (; end < compound.length; end += 1) {
          if (compound[end] === "(") depth += 1;
          else if (compound[end] === ")") {
            depth -= 1;
            if (depth === 0) break;
          }
        }
        const inner = compound.slice(index + 5, end);
        if (matchCompound(el, inner)) return false;
        index = end + 1;
        saw = true;
      } else {
        return false;
      }
    } else if (/[a-z*]/i.test(ch)) {
      const tag = identAt(compound, index);
      if (tag.name !== "*" && el.tag !== tag.name) return false;
      index = tag.next;
      saw = true;
    } else {
      index += 1;
    }
  }
  return saw;
}

function matchesSelector(el, selector) {
  const parts = selector.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return false;
  let node = el;
  if (!matchCompound(node, parts[parts.length - 1])) return false;
  for (let part = parts.length - 2; part >= 0; part -= 1) {
    node = node.parent;
    let found = false;
    while (node) {
      if (matchCompound(node, parts[part])) {
        found = true;
        break;
      }
      node = node.parent;
    }
    if (!found) return false;
  }
  return true;
}

function specificity(selector) {
  const ids = (selector.match(/#[\w-]+/g) || []).length;
  const classes = (selector.match(/\.[\w-]+/g) || []).length;
  const attrs = (selector.match(/\[[^\]]+\]/g) || []).length;
  const stripped = selector
    .replace(/#[\w-]+/g, " ")
    .replace(/\.[\w-]+/g, " ")
    .replace(/\[[^\]]+\]/g, " ")
    .replace(/:[\w-]+(?:\([^)]*\))?/g, " ");
  const elements = stripped.split(/[\s>+~]+/).filter((part) => part && part !== "*").length;
  return [ids, classes + attrs, elements];
}

function authorDisplayRules(sheet) {
  const rules = [];
  const pattern = /([^{}@]+)\{([^{}]*)\}/g;
  let found;
  while ((found = pattern.exec(sheet))) {
    const bodyText = found[2];
    const declared = bodyText.match(/display\s*:\s*([^;]+)/);
    if (!declared) continue;
    const important = /display\s*:[^;]*!important/.test(bodyText);
    const value = declared[1].replace(/!important/g, "").trim();
    for (const raw of found[1].split(",")) {
      const selector = raw.trim();
      if (!selector) continue;
      rules.push({ selector, value, important, specificity: specificity(selector), order: rules.length });
    }
  }
  return rules;
}

function computedDisplay(el, sheet) {
  const initial = { div: "block", p: "block", label: "inline", button: "inline-block", span: "inline" }[el.tag] || "inline";
  let winner = { value: initial, rank: [-1, 0, 0, 0, -1] };
  const rules = [
    { selector: "[hidden]", value: "none", important: false, specificity: [0, 1, 0], order: -1, origin: 0 },
    ...authorDisplayRules(sheet).map((rule) => ({ ...rule, origin: 1 })),
  ];
  for (const rule of rules) {
    if (!matchesSelector(el, rule.selector)) continue;
    const importance = rule.important ? 2 : rule.origin === 0 ? 0 : 1;
    const rank = [importance, ...rule.specificity, rule.order];
    let better = false;
    for (let i = 0; i < rank.length; i += 1) {
      if (rank[i] === winner.rank[i]) continue;
      better = rank[i] > winner.rank[i];
      break;
    }
    if (better) winner = { value: rule.value, rank };
  }
  return winner.value;
}

function element(fields) {
  return {
    tag: fields.tag,
    id: fields.id || "",
    classes: fields.classes || new Set(),
    hidden: !!fields.hidden,
    attrs: fields.attrs || {},
    parent: fields.parent || null,
  };
}

const page = element({ tag: "body" });
const drawer = element({ tag: "div", id: "drawer", hidden: true, parent: page });
assert.equal(computedDisplay(drawer, style), "none", "collapsed drawer must not paint");
drawer.hidden = false;
assert.equal(computedDisplay(drawer, style), "flex", "opening the drawer shows the controls");
drawer.hidden = true;
assert.equal(computedDisplay(drawer, style), "none", "closing the drawer hides it again");

const wakeLabel = element({ tag: "label", id: "wake-label", hidden: true, parent: drawer });
const wakeHelp = element({ tag: "p", id: "wake-help", hidden: false, parent: drawer });
assert.equal(computedDisplay(wakeLabel, style), "none", "unsupported wake lock hides the dead toggle");
assert.notEqual(computedDisplay(wakeHelp, style), "none", "unsupported wake lock keeps the note");
wakeLabel.hidden = false;
wakeHelp.hidden = true;
assert.notEqual(computedDisplay(wakeLabel, style), "none");
assert.equal(computedDisplay(wakeHelp, style), "none");

async function bootAudienceModule(label, blockStorage) {
  const sourceMatch = html.match(/<script type="module">([\s\S]*?)<\/script>/);
  assert.ok(sourceMatch, "module script");
  const staticRoot = new URL("../app/static/", import.meta.url).href;
  const rewritten = sourceMatch[1].replaceAll('"/static/', '"' + staticRoot);
  const dir = mkdtempSync(`${tmpdir()}/room-boot-`);
  const file = `${dir}/${label}.mjs`;
  writeFileSync(file, rewritten);
  const sockets = [];
  const previous = {
    document: globalThis.document,
    window: globalThis.window,
    location: globalThis.location,
    WebSocket: globalThis.WebSocket,
    navigator: globalThis.navigator,
    setInterval: globalThis.setInterval,
    setTimeout: globalThis.setTimeout,
  };
  const els = new Map();
  function el(id) {
    if (els.has(id)) return els.get(id);
    const classes = new Set();
    const node = {
      id,
      hidden: false,
      value: "",
      checked: false,
      scrollTop: 0,
      scrollHeight: 0,
      clientHeight: 0,
      children: [],
      attrs: {},
      _text: "",
      classList: {
        add(name) { classes.add(name); },
        remove(name) { classes.delete(name); },
        contains(name) { return classes.has(name); },
        toggle(name, on) {
          const next = on == null ? !classes.has(name) : !!on;
          if (next) classes.add(name);
          else classes.delete(name);
          return next;
        },
      },
      setAttribute(key, value) { this.attrs[key] = String(value); },
      getAttribute(key) { return Object.prototype.hasOwnProperty.call(this.attrs, key) ? this.attrs[key] : null; },
      appendChild(child) { this.children.push(child); return child; },
      append(...children) { this.children.push(...children); },
      addEventListener() {},
      removeEventListener() {},
      focus() {},
      requestFullscreen() { return Promise.resolve(); },
    };
    Object.defineProperty(node, "textContent", {
      get() { return node._text; },
      set(value) { node._text = String(value); node.children = []; },
    });
    els.set(id, node);
    return node;
  }
  const memory = () => {
    const data = new Map();
    return {
      getItem(key) { return data.has(key) ? data.get(key) : null; },
      setItem(key, value) { data.set(key, String(value)); },
      removeItem(key) { data.delete(key); },
    };
  };
  const local = memory();
  const session = memory();
  globalThis.document = {
    visibilityState: "visible",
    fullscreenEnabled: false,
    fullscreenElement: null,
    body: el("body"),
    documentElement: el("html"),
    querySelector: (sel) => el(sel),
    createElement: () => el("new-" + Math.random()),
    createTextNode: (text) => ({ text }),
    addEventListener() {},
    removeEventListener() {},
    exitFullscreen() { return Promise.resolve(); },
  };
  const win = {
    addEventListener() {},
    removeEventListener() {},
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  };
  Object.defineProperty(win, "localStorage", {
    get() { if (blockStorage) throw new Error("SecurityError"); return local; },
  });
  Object.defineProperty(win, "sessionStorage", {
    get() { if (blockStorage) throw new Error("SecurityError"); return session; },
  });
  globalThis.window = win;
  Object.defineProperty(globalThis, "navigator", { value: { onLine: true }, configurable: true, writable: true });
  globalThis.location = { pathname: "/r/class", search: "?k=KEY", protocol: "http:", host: "127.0.0.1:8780" };
  globalThis.WebSocket = class {
    constructor(url) { this.url = url; this.readyState = 0; sockets.push(this); }
    send() {}
    close() { this.readyState = 3; }
  };
  globalThis.setInterval = (fn, ms, ...args) => {
    const id = previous.setInterval(fn, ms, ...args);
    if (id && typeof id.unref === "function") id.unref();
    return id;
  };
  globalThis.setTimeout = (fn, ms, ...args) => {
    const id = previous.setTimeout(fn, ms, ...args);
    if (id && typeof id.unref === "function") id.unref();
    return id;
  };
  let error = null;
  try {
    await import(pathToFileURL(file).href);
  } catch (err) {
    error = err;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
  await new Promise((resolve) => previous.setTimeout(resolve, 0));
  globalThis.setInterval = previous.setInterval;
  globalThis.setTimeout = previous.setTimeout;
  return { sockets: sockets.length, error };
}

const openPage = await bootAudienceModule("storage-open", false);
assert.equal(openPage.error, null, openPage.error && openPage.error.stack);
assert.equal(openPage.sockets, 1, "page load must open exactly one socket");
const blockedPage = await bootAudienceModule("storage-blocked", true);
assert.equal(blockedPage.error, null, blockedPage.error && blockedPage.error.stack);
assert.equal(blockedPage.sockets, 1, "blocked storage must still open exactly one socket");

console.log("room dom ok");
