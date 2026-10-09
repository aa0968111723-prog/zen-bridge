import assert from "node:assert/strict";
import { contrast } from "../app/static/room_view.js";
import {
  DEFAULT_PREFS,
  PREFS_KEY,
  appearance,
  fullscreenAvailable,
  focusAfterProjection,
  leaveProjection,
  readPrefs,
  refreshWakeLock,
  releaseWakeLock,
  requestWakeLock,
  safeStorage,
  sanitizePrefs,
  themeIsDark,
  wakeLockSupported,
  writePrefs,
} from "../app/static/room_prefs.js";

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return {
    getItem(key) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null;
    },
    setItem(key, value) {
      data[key] = String(value);
    },
  };
}

assert.equal(PREFS_KEY, "breeze.audience.prefs");
assert.equal(safeStorage(() => { throw new Error("SecurityError"); }), null);
assert.equal(safeStorage(() => null), null);
const mem = safeStorage(() => memoryStorage({ kept: "1" }));
assert.equal(mem.getItem("kept"), "1");
assert.deepEqual(readPrefs(null), { ...DEFAULT_PREFS });
assert.equal(sanitizePrefs({ wake: false }).wake, false);
assert.equal(sanitizePrefs({}).wake, true);
assert.equal(sanitizePrefs({ wake: "false" }).wake, true);
assert.equal(sanitizePrefs({ mode: "project" }).project, true);
assert.equal(sanitizePrefs({ mode: "en", project: true }).project, false);
assert.equal(sanitizePrefs({ mode: "nope", size: 12, theme: "blue" }).mode, "both");
assert.equal(sanitizePrefs({ size: 64 }).size, "64");
assert.equal(sanitizePrefs({ size: "64px" }).size, "34");

const store = memoryStorage();
const saved = writePrefs(store, { mode: "project", size: "64", theme: "dark", wake: true });
assert.deepEqual(saved, { mode: "project", size: "64", theme: "dark", wake: true, project: true });
assert.deepEqual(readPrefs(store), saved);
assert.equal(store.getItem(PREFS_KEY).includes('"mode":"project"'), true);

writePrefs(store, { mode: "nope", size: "12", theme: "neon", wake: false, project: true });
assert.deepEqual(readPrefs(store), { mode: "both", size: "34", theme: "system", wake: false, project: false });

store.setItem(PREFS_KEY, "{");
assert.deepEqual(readPrefs(store), { ...DEFAULT_PREFS });
store.setItem(PREFS_KEY, "null");
assert.deepEqual(readPrefs(store), { ...DEFAULT_PREFS });

const broken = {
  getItem() {
    return null;
  },
  setItem() {
    throw new Error("quota");
  },
};
assert.equal(writePrefs(broken, { mode: "zh" }).mode, "zh");
assert.deepEqual(readPrefs({ getItem() { throw new Error("denied"); } }), { ...DEFAULT_PREFS });

assert.equal(themeIsDark("dark", () => ({ matches: false })), true);
assert.equal(themeIsDark("light", () => ({ matches: true })), false);
assert.equal(themeIsDark("system", () => ({ matches: true })), true);
assert.equal(themeIsDark("system", () => ({ matches: false })), false);
assert.equal(themeIsDark("system", null), false);
assert.equal(themeIsDark("system", () => { throw new Error("no media"); }), false);

assert.deepEqual(appearance({ mode: "project", size: "64", theme: "light" }, true), ["size-64", "project"]);
assert.ok(appearance({ theme: "system", mode: "en" }, true).includes("dark"));
assert.ok(appearance({ theme: "system", mode: "en" }, true).includes("en-only"));
assert.equal(appearance({ theme: "system" }, false).includes("dark"), false);
assert.ok(appearance({ theme: "dark", mode: "zh" }, false).includes("zh-only"));
assert.equal(appearance({ mode: "both", size: "28" }, false).includes("project"), false);

assert.equal(wakeLockSupported(undefined), false);
assert.equal(wakeLockSupported({}), false);
assert.equal(wakeLockSupported({ wakeLock: {} }), false);
assert.equal(await requestWakeLock(undefined), null);
assert.equal(await requestWakeLock({}), null);
assert.equal(await requestWakeLock({ wakeLock: { request: async () => { throw new Error("denied"); } } }), null);

let requests = 0;
const nav = {
  wakeLock: {
    async request(kind) {
      requests += 1;
      assert.equal(kind, "screen");
      return { kind, released: false, release() { this.released = true; } };
    },
  },
};
const first = await refreshWakeLock(nav, { wake: true, mode: "both" }, "visible");
const second = await refreshWakeLock(nav, { wake: true }, "visible");
assert.equal(requests, 2);
assert.equal(first.kind, "screen");
assert.ok(second);
assert.equal(await refreshWakeLock(nav, { wake: true }, "hidden"), null);
assert.equal(await refreshWakeLock(nav, { wake: false }, "visible"), null);
assert.equal(requests, 2);
assert.equal(await refreshWakeLock({}, { wake: true }, "visible"), null);
await releaseWakeLock(first);
assert.equal(first.released, true);
await releaseWakeLock(null);
await releaseWakeLock({ release() { throw new Error("gone"); } });

const project = { mode: "project", size: "46", theme: "system", wake: true, project: true };
const left = leaveProjection(project, "Escape");
assert.equal(left.mode, "both");
assert.equal(left.project, false);
assert.equal(left.size, "46");
assert.equal(leaveProjection(project, "Enter"), null);
assert.equal(leaveProjection({ mode: "en" }, "Escape"), null);
assert.equal(leaveProjection(null, "Escape"), null);

assert.equal(fullscreenAvailable({ fullscreenEnabled: false }), false);
assert.equal(fullscreenAvailable({ fullscreenEnabled: true }), true);
assert.equal(fullscreenAvailable(undefined), false);

const opener = { focused: false, focus() { this.focused = true; } };
assert.equal(focusAfterProjection(opener), true);
assert.equal(opener.focused, true);
assert.equal(focusAfterProjection(null), false);
assert.equal(focusAfterProjection({}), false);
assert.equal(focusAfterProjection({ focus() { throw new Error("hidden"); } }), false);

assert.ok(contrast("#f6f1e7", "#16130f") >= 4.5);
assert.ok(contrast("#d9d0c3", "#16130f") >= 4.5);

console.log("room prefs ok");
