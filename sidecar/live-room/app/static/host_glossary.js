// Host glossary save. A failed legacy POST must not look like a successful write.
// The textarea speaks zh|alias=en. Aliases that round-trip stay editable.
// Lock off, a note, a category, text the line would change, or more than 40 rows do not.

const LEGACY_BOX_LIMIT = 40;
const LEGACY_MAX_EN = 80;
// str.isspace() in the Python that parses a legacy post. String.trim() also
// strips U+FEFF, which this set does not, and it misses U+001C–U+001F and U+0085.
const PYTHON_SPACE = new Set([
  0x0009, 0x000a, 0x000b, 0x000c, 0x000d, 0x001c, 0x001d, 0x001e, 0x001f,
  0x0020, 0x0085, 0x00a0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004,
  0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f,
  0x205f, 0x3000,
]);
const LEGACY_EDIT_PLACE = "請用 PUT /api/rooms/{room_id}/glossary 修改。這是技術操作，請找負責詞表的人。格式見 README「修改房間術語表」。";
const LEGACY_RICH = "含備註、分類或未鎖定的詞，或文字框無法原樣表示的內容";

export function glossarySaveLine(status, body) {
  if (Number(status) === 200) {
    const deleted = body && Number(body.deleted);
    if (Number.isInteger(deleted) && deleted > 0) {
      return "已儲存這個房間的術語。這次刪了 " + deleted + " 條。";
    }
    return "已儲存這個房間的術語。";
  }
  const rejected = body && Array.isArray(body.rejected) ? body.rejected : [];
  const details = [];
  for (const item of rejected) {
    if (!item || typeof item !== "object") continue;
    const reason = typeof item.reason === "string" ? item.reason : "";
    if (!reason) continue;
    const lineNo = Number(item.line);
    details.push(lineNo > 0 ? "第 " + lineNo + " 行：" + reason : reason);
  }
  const lines = ["術語沒有寫入這個房間。"];
  if (Number(status) === 409) lines.push("術語表已更新，請重新整理頁面後再儲存。");
  lines.push(...details);
  if (lines.length === 1) return lines[0];
  return lines.join("\n");
}

export function glossaryTransportLine(err) {
  const detail = err && typeof err.message === "string" && err.message ? "（" + err.message + "）" : "";
  return "術語沒有寫入這個房間。" + detail;
}

export function glossaryPostBody(room, sessionId, text, version) {
  const body = {
    room_id: room,
    session_id: sessionId,
    text: String(text ?? ""),
  };
  if (Number.isInteger(version)) body.if_version = version;
  return body;
}

export function glossaryBoxText(terms) {
  const lines = [];
  for (const term of terms || []) {
    if (!term || typeof term !== "object") continue;
    const zh = typeof term.zh === "string" ? term.zh : "";
    const en = typeof term.en === "string" ? term.en : "";
    const aliases = Array.isArray(term.aliases)
      ? term.aliases.filter((alias) => typeof alias === "string" && alias)
      : [];
    const left = aliases.length ? [zh, ...aliases].join("|") : zh;
    lines.push(left + "=" + en);
  }
  return lines.join("\n");
}

function glossaryPythonStrip(text) {
  let start = 0;
  let end = text.length;
  while (start < end && PYTHON_SPACE.has(text.charCodeAt(start))) start += 1;
  while (end > start && PYTHON_SPACE.has(text.charCodeAt(end - 1))) end -= 1;
  return text.slice(start, end);
}

function glossaryHasInvisibleBreak(text) {
  return text.includes("\u0085") || text.includes("\u2028") || text.includes("\u2029");
}

// First ASCII "=" wins, including when a fullwidth equals sits later in the English.
function glossarySplitEquals(text) {
  const ascii = text.indexOf("=");
  if (ascii >= 0) return [text.slice(0, ascii), text.slice(ascii + 1)];
  const full = text.indexOf("＝");
  if (full >= 0) return [text.slice(0, full), text.slice(full + 1)];
  return null;
}

// Text the legacy POST stores, rewritten with glossaryBoxText.
// strict_legacy_rows drops blank lines and "#" comments, strips each field, splits
// on the first "=" or "＝", and keeps alias pieces. validate_terms then drops
// duplicate aliases. "\n" is the only line break; a trailing "\r" is stripped
// with the field. U+0085 / U+2028 / U+2029 reject the whole body, and so does a
// line that would not be stored (no equals, an empty alias, an empty side, a
// 41st term, English over 80). Those stay unchanged: rewriting them into fewer
// terms would match a different glossary and adopt its version.
export function glossaryCanonicalText(raw) {
  const text = String(raw ?? "");
  if (glossaryHasInvisibleBreak(text)) return text;
  const rows = [];
  for (const line of text.split("\n")) {
    const trimmed = glossaryPythonStrip(line);
    if (!trimmed || trimmed.startsWith("#")) continue;
    const split = glossarySplitEquals(trimmed);
    if (!split) return text;
    const en = glossaryPythonStrip(split[1]);
    const aliases = [];
    let zh;
    if (split[0].includes("|")) {
      const parts = split[0].split("|");
      zh = glossaryPythonStrip(parts[0]);
      for (let i = 1; i < parts.length; i += 1) {
        const alias = glossaryPythonStrip(parts[i]);
        if (!alias) return text;
        if (!aliases.includes(alias)) aliases.push(alias);
      }
    } else {
      zh = glossaryPythonStrip(split[0]);
    }
    if (!zh || !en) return text;
    if (rows.length >= LEGACY_BOX_LIMIT) return text;
    if ([...en].length > LEGACY_MAX_EN) return text;
    rows.push({ zh, en, aliases });
  }
  if (!rows.length) return text;
  return glossaryBoxText(rows);
}

function glossaryLineBreaks(text) {
  return /[\n\r\u0085\u2028\u2029]/.test(text);
}

// The left side is split on | and the line is split on the first = or ＝.
// English is everything after that first equals, so = | ＝ inside en still round-trip.
function glossaryLeftSeparators(text) {
  return /[|=\uFF1D]/.test(text) || glossaryLineBreaks(text);
}

function glossaryTermUnexpressable(term) {
  if (!term || typeof term !== "object") return false;
  if (term.lock === false) return true;
  if (typeof term.note === "string" && term.note.trim()) return true;
  if (typeof term.category === "string" && term.category.trim()) return true;
  const zh = typeof term.zh === "string" ? term.zh : "";
  const en = typeof term.en === "string" ? term.en : "";
  if (zh.trim() !== zh || en.trim() !== en || glossaryLeftSeparators(zh) || glossaryLineBreaks(en)) return true;
  // A leading # is a comment in the textarea, so the canonical would not round-trip.
  if (zh.startsWith("#")) return true;
  const aliases = Array.isArray(term.aliases) ? term.aliases : [];
  for (const alias of aliases) {
    if (typeof alias !== "string" || !alias) continue;
    if (alias.trim() !== alias || !alias.trim() || glossaryLeftSeparators(alias)) return true;
  }
  return false;
}

export function glossaryHasAdvanced(terms) {
  for (const term of terms || []) {
    if (glossaryTermUnexpressable(term)) return true;
  }
  return false;
}

function glossaryRoomName(room) {
  return String(room || "").trim() || "class";
}

// 0 is the first visit. A room change advances it; a same-room reconnect does not.
function glossaryVisitGeneration(state) {
  const visit = state && state.visitGeneration;
  return Number.isInteger(visit) && visit >= 0 ? visit : 0;
}

function glossaryEditPlace(room) {
  if (room == null) return LEGACY_EDIT_PLACE;
  return LEGACY_EDIT_PLACE.replace("{room_id}", glossaryRoomName(room));
}

function glossaryPrefillNote(count) {
  return "這個房間已經存了 " + count + " 條術語，但文字框裡是開頁前留下的內容，跟已存的不一樣。為了不蓋掉那 " + count + " 條，現在不能儲存。要看已存的詞表：先把文字框裡想留的詞複製起來，清空文字框，再重新整理頁面。";
}

function glossaryRefused(note) {
  if (!note || note.startsWith("沒有儲存：")) return note || "";
  return "沒有儲存：" + note;
}

export function glossaryLegacyBlock(terms, room) {
  const list = Array.isArray(terms) ? terms : [];
  const advanced = glossaryHasAdvanced(list);
  const over = list.length > LEGACY_BOX_LIMIT;
  const limit = "（主持頁最多 " + LEGACY_BOX_LIMIT + " 條）";
  const place = glossaryEditPlace(room);
  if (over && advanced) {
    return {
      locked: true,
      note: "這個房間的術語表有 " + list.length + " 條" + limit + "，而且" + LEGACY_RICH + "，這裡只能看、不能改。" + place,
    };
  }
  if (over) {
    return {
      locked: true,
      note: "這個房間的術語表有 " + list.length + " 條" + limit + "，這裡只能看、不能改。" + place,
    };
  }
  if (advanced) {
    return {
      locked: true,
      note: "這個房間的術語表" + LEGACY_RICH + "，這裡只能看、不能改。" + place,
    };
  }
  return { locked: false, note: "" };
}

export function glossarySaveRequest(room, sessionId, text, loadedVersion, locked) {
  if (locked) return { post: false, reason: "locked" };
  if (!String(text ?? "").trim()) return { post: false, reason: "empty" };
  // loadedVersion is the version captured when the box was filled, not a fresh GET.
  if (!Number.isInteger(loadedVersion)) return { post: false, reason: "no-version" };
  return { post: true, body: glossaryPostBody(room, sessionId, text, loadedVersion) };
}

export function glossaryRoomState() {
  return {
    room: null,
    version: null,
    locked: false,
    text: "",
    loadedText: "",
    note: "",
    readOnly: false,
    generation: 0,
    pendingRoom: null,
    // True when the box held text before the first load of a non-empty glossary.
    // That text must not be given the server version.
    prefillUnversioned: false,
    visitGeneration: 0,
  };
}

export function glossaryTextDirty(state) {
  return !!state && state.text !== state.loadedText;
}

export function glossaryEdit(state, text) {
  return { ...state, text: String(text ?? "") };
}

export function glossaryMarkSaved(state) {
  // Drop the version until the following load reads the one the server just wrote.
  return { ...state, loadedText: state.text, version: null, prefillUnversioned: false };
}

export function glossaryPrepareLoad(state, room) {
  const nextRoom = glossaryRoomName(room);
  const generation = state.generation + 1;
  if (state.room === nextRoom || state.room == null) {
    return { ...state, generation, pendingRoom: nextRoom };
  }
  return {
    room: null,
    version: null,
    locked: true,
    text: "",
    loadedText: "",
    note: "正在讀取這個房間的術語。",
    readOnly: true,
    generation,
    pendingRoom: nextRoom,
    prefillUnversioned: false,
    visitGeneration: generation,
  };
}

export function glossaryLoadTargetsSelector(requestedRoom, selectorRoom) {
  return glossaryRoomName(requestedRoom) === glossaryRoomName(selectorRoom);
}

// A GET for a room the selector is not showing must not clear the box.
export function glossaryBeginLoad(state, requestedRoom, selectorRoom) {
  if (!glossaryLoadTargetsSelector(requestedRoom, selectorRoom)) {
    return { started: false, state, generation: state.generation };
  }
  const next = glossaryPrepareLoad(state, requestedRoom);
  return {
    started: true,
    state: next,
    generation: next.generation,
    room: glossaryRoomName(requestedRoom),
  };
}

export function glossaryApplyLoaded(state, room, generation, terms, version) {
  const nextRoom = glossaryRoomName(room);
  if (generation !== state.generation || state.pendingRoom !== nextRoom) return state;
  const block = glossaryLegacyBlock(terms, nextRoom);
  const serverText = glossaryBoxText(terms);
  const dirty = state.text !== state.loadedText;
  const count = Array.isArray(terms) ? terms.length : 0;
  if (dirty && state.room === nextRoom) {
    if (state.prefillUnversioned) {
      return { ...state, version: null, note: glossaryPrefillNote(count) };
    }
    // version is null because this visit just posted loadedText. The reload's
    // version is the base of keystrokes typed since only when the server body
    // is that posted text and the glossary is still editable. A different body
    // or a locked one can hold words the box does not show. Compare the text
    // the legacy POST stores, not the raw textarea, so a trailing newline is
    // still the same words.
    const adoptPostedVersion = state.version == null
      && serverText === glossaryCanonicalText(state.loadedText)
      && !state.locked
      && !block.locked;
    return {
      ...state,
      version: adoptPostedVersion ? version : state.version,
      note: "這個文字框有還沒儲存的修改，沒有用已經存好的詞蓋掉。",
    };
  }
  // First paint can already hold typed or browser-restored text. Pairing that
  // text with the server version lets a save replace terms the host has not seen.
  if (dirty && state.room == null && !block.locked) {
    if (count > 0 && state.text !== serverText) {
      return {
        room: nextRoom,
        version: null,
        locked: false,
        text: state.text,
        loadedText: serverText,
        note: glossaryPrefillNote(count),
        readOnly: false,
        generation,
        pendingRoom: nextRoom,
        prefillUnversioned: true,
        visitGeneration: glossaryVisitGeneration(state),
      };
    }
    return {
      room: nextRoom,
      version,
      locked: false,
      text: state.text,
      loadedText: serverText,
      note: "這個文字框有還沒儲存的修改，沒有用已經存好的詞蓋掉。",
      readOnly: false,
      generation,
      pendingRoom: nextRoom,
      prefillUnversioned: false,
      visitGeneration: glossaryVisitGeneration(state),
    };
  }
  return {
    room: nextRoom,
    version,
    locked: block.locked,
    text: serverText,
    loadedText: serverText,
    note: block.note || "",
    readOnly: block.locked,
    generation,
    pendingRoom: nextRoom,
    prefillUnversioned: false,
    visitGeneration: glossaryVisitGeneration(state),
  };
}

export function glossaryApplyLoadFailure(state, room, generation) {
  const nextRoom = glossaryRoomName(room);
  if (generation !== state.generation || state.pendingRoom !== nextRoom) return state;
  const dirty = state.text !== state.loadedText;
  if (state.room === nextRoom || (state.room == null && dirty)) {
    const note = dirty
      ? "讀不到這個房間已經存的術語。文字框裡還沒儲存的修改還在。"
      : "讀不到這個房間已經存的術語，畫面上仍是上次看到的內容。";
    return { ...state, note };
  }
  return {
    room: null,
    version: null,
    locked: true,
    text: "",
    loadedText: "",
    note: "讀不到這個房間已經存的術語，所以這裡先空白。",
    readOnly: true,
    generation,
    pendingRoom: nextRoom,
    prefillUnversioned: false,
    visitGeneration: glossaryVisitGeneration(state),
  };
}

export function glossarySaveDecision(state, selectorRoom, sessionId) {
  const target = glossaryRoomName(selectorRoom);
  if (!state.room || target !== state.room) return { post: false, reason: "room-mismatch" };
  if (state.locked) return { post: false, reason: "locked" };
  return glossarySaveRequest(state.room, sessionId, state.text, state.version, false);
}

// A 200 applies only to the visit that posted. Another room is left untouched.
// Returning to the same room starts a new visit (visitGeneration is newer than
// the save), so that visit's loaded text and version stay. The same visit uses
// the posted text, not keystrokes typed since, drops the stale version, and
// reloads. Otherwise the next save sends the old if_version and gets 409.
function glossaryRememberPosted(state, sentText) {
  const loadedText = sentText == null ? state.text : String(sentText);
  return { ...state, loadedText, version: null, prefillUnversioned: false };
}

export function glossarySaveSettlement(state, requestRoom, requestGeneration, sentText) {
  const room = glossaryRoomName(requestRoom);
  if (!state || state.room !== room) {
    return { settle: false, state };
  }
  if (requestGeneration < glossaryVisitGeneration(state)) {
    return { settle: false, state };
  }
  return { settle: true, state: glossaryRememberPosted(state, sentText), reloadRoom: room };
}

export function glossaryRefusal(state, reason) {
  if (reason === "locked") {
    return (state && state.note) || ("這裡只能看、不能改。" + glossaryEditPlace(state && state.room));
  }
  if (reason === "room-mismatch") {
    if (state && state.note) return glossaryRefused(state.note);
    return "沒有儲存：文字框裡的詞不是這個房間的。";
  }
  if (reason === "no-version" && state && state.prefillUnversioned && state.note) {
    return glossaryRefused(state.note);
  }
  return "還沒讀到這個房間目前存的術語，請重新整理頁面後再儲存。";
}
