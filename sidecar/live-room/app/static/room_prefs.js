// Audience display preferences. No socket and no DOM, so node tests can call them.

export const PREFS_KEY = "breeze.audience.prefs";

export const DEFAULT_PREFS = Object.freeze({
  mode: "both",
  size: "34",
  theme: "system",
  wake: true,
  project: false,
});

const MODES = new Set(["both", "zh", "en", "project"]);
const SIZES = new Set(["28", "34", "46", "64"]);
const THEMES = new Set(["system", "light", "dark"]);

export function sanitizePrefs(raw) {
  const source = raw && typeof raw === "object" ? raw : {};
  const mode = MODES.has(source.mode) ? source.mode : DEFAULT_PREFS.mode;
  const size = SIZES.has(String(source.size)) ? String(source.size) : DEFAULT_PREFS.size;
  const theme = THEMES.has(source.theme) ? source.theme : DEFAULT_PREFS.theme;
  const wake = source.wake === false ? false : true;
  return { mode, size, theme, wake, project: mode === "project" };
}

// Reading window.localStorage itself throws when storage is blocked.
// Callers pass a getter so the throw happens inside this try.
export function safeStorage(read) {
  try {
    const value = typeof read === "function" ? read() : read;
    return value == null ? null : value;
  } catch {
    return null;
  }
}

export function readPrefs(storage) {
  if (!storage || typeof storage.getItem !== "function") return sanitizePrefs(null);
  try {
    const raw = storage.getItem(PREFS_KEY);
    if (!raw) return sanitizePrefs(null);
    return sanitizePrefs(JSON.parse(raw));
  } catch {
    return sanitizePrefs(null);
  }
}

export function writePrefs(storage, prefs) {
  const clean = sanitizePrefs(prefs);
  if (storage && typeof storage.setItem === "function") {
    try {
      storage.setItem(PREFS_KEY, JSON.stringify(clean));
    } catch {
      // Private mode or a full disk: keep the choice for this page view.
    }
  }
  return clean;
}

export function themeIsDark(theme, matchMedia) {
  if (theme === "dark") return true;
  if (theme === "light") return false;
  if (typeof matchMedia !== "function") return false;
  try {
    return !!matchMedia("(prefers-color-scheme: dark)").matches;
  } catch {
    return false;
  }
}

export function appearance(prefs, prefersDark) {
  const clean = sanitizePrefs(prefs);
  const dark = clean.theme === "dark" || (clean.theme === "system" && !!prefersDark);
  const classes = ["size-" + clean.size];
  if (clean.mode === "zh") classes.push("zh-only");
  if (clean.mode === "en") classes.push("en-only");
  if (clean.mode === "project") classes.push("project");
  if (dark) classes.push("dark");
  return classes;
}

export function wakeLockSupported(nav) {
  return !!(nav && nav.wakeLock && typeof nav.wakeLock.request === "function");
}

export async function requestWakeLock(nav) {
  if (!wakeLockSupported(nav)) return null;
  try {
    return await nav.wakeLock.request("screen");
  } catch {
    return null;
  }
}

export async function releaseWakeLock(sentinel) {
  if (!sentinel || typeof sentinel.release !== "function") return;
  try {
    await sentinel.release();
  } catch {
    // Already released when the page was hidden.
  }
}

// Ask again only while the page is visible and the listener left the switch on.
export async function refreshWakeLock(nav, prefs, visibility) {
  if (visibility !== "visible") return null;
  if (!prefs || prefs.wake === false) return null;
  return requestWakeLock(nav);
}

export function leaveProjection(prefs, key) {
  if (key !== "Escape" || !prefs || prefs.mode !== "project") return null;
  return sanitizePrefs({ ...prefs, mode: "both" });
}

export function fullscreenAvailable(doc) {
  return !!(doc && doc.fullscreenEnabled);
}

// Projection is left from the header or Escape. Focus goes back to the button that opened it.
export function focusAfterProjection(opener) {
  if (!opener || typeof opener.focus !== "function") return false;
  try {
    opener.focus();
    return true;
  } catch {
    return false;
  }
}
