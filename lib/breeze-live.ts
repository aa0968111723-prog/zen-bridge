// Breeze Live Room <-> 禪譯 turtle UI: pure caption logic shared by the live panel and tests.
// Mirrors sidecar/live-room/app/static/room_client.js (createCaptionStore) and room_target.js
// (paintTarget) so the turtle UI shows exactly what the audience page shows.

export type TargetLang = 'en' | 'ja';
export type RubyTerm = { text: string; reading: string };
export type BreezeEvent = {
  type?: string; id?: string; seq?: number; session_id?: string; room_id?: string; session_ord?: number;
  zh?: string; en?: string; status?: string; version?: number; tgt_lang?: string; segments?: string[];
  ruby?: RubyTerm[]; epoch?: number; ids?: string[]; reason?: string; history?: BreezeEvent[];
};
export type CaptionLine = {
  id: string; seq: number; session_id: string; session_ord: number; zh: string; en: string;
  status: string; draft: boolean; version: number; tgt_lang: TargetLang; segments?: string[]; ruby?: RubyTerm[];
};

export const TARGET_LANGS: { value: TargetLang; label: string; native: string }[] = [
  { value: 'en', label: '英文 English', native: 'English' },
  { value: 'ja', label: '日文 日本語', native: '日本語' },
];
export const JA_FONT = '"Yu Gothic UI","Meiryo","Hiragino Sans","Noto Sans JP",sans-serif';

export function isTargetLang(value: unknown): value is TargetLang { return value === 'en' || value === 'ja'; }
export function targetLangOf(item: { tgt_lang?: unknown } | null | undefined, fallback: TargetLang = 'en'): TargetLang {
  return item && isTargetLang(item.tgt_lang) ? item.tgt_lang : fallback;
}

/** One language per session: the choice is free until the session starts, then locked. */
export function canChangeTarget(sessionActive: boolean, current: TargetLang, next: string): { ok: boolean; reason?: string } {
  if (!isTargetLang(next)) return { ok: false, reason: '只支援英文或日文' };
  if (next === current) return { ok: true };
  if (sessionActive) return { ok: false, reason: '本場已開始，目標語言已鎖定；請結束本場後再換語言。' };
  return { ok: true };
}

function isFinalKind(kind: unknown) { return kind == null || kind === '' || kind === 'caption' || kind === 'final'; }

/** Caption store: grey drafts are replaced in place by the final with the same id; finals are never
 *  overwritten by a draft; deletes and clears are honoured. */
export function createCaptionStore(limit = 200) {
  const items = new Map<string, CaptionLine>();
  const deleted = new Set<string>();
  let epochFloor = 0;
  function apply(item: BreezeEvent | null | undefined): boolean {
    if (!item || typeof item !== 'object') return false;
    if (item.type === 'captions_cleared') {
      for (const id of items.keys()) deleted.add(id);
      items.clear();
      const epoch = Number(item.epoch); if (Number.isFinite(epoch)) epochFloor = Math.max(epochFloor, epoch);
      return true;
    }
    if (item.type === 'captions_expired') {
      for (const id of Array.isArray(item.ids) ? item.ids : []) items.delete(id);
      if (item.id) items.delete(item.id);
      return true;
    }
    const epoch = Number(item.epoch);
    if (Number.isFinite(epoch) && epochFloor && epoch < epochFloor) return false;
    if (!item.id) return false;
    if (item.type === 'caption_deleted') { deleted.add(item.id); return items.delete(item.id); }
    if (deleted.has(item.id) || item.seq == null) return false;
    if (item.type === 'draft') {
      const have = items.get(item.id);
      if (have && !have.draft) return false;
      let ord = Number(item.session_ord) || 0;
      if (!ord) for (const row of items.values()) if (row.session_id === item.session_id && row.session_ord > ord) ord = row.session_ord;
      items.set(item.id, { id: item.id, seq: Number(item.seq), session_id: String(item.session_id || ''), session_ord: ord,
        zh: String(item.zh || ''), en: '', status: 'draft', draft: true, version: 0, tgt_lang: targetLangOf(item) });
      trim(); return true;
    }
    if (!isFinalKind(item.type)) return false;
    const prev = items.get(item.id);
    const base = prev && !prev.draft ? prev : undefined;
    const version = Number(item.version) || 0;
    if (base && version && base.version > version) return false;
    const next: CaptionLine = {
      id: item.id, seq: Number(item.seq), session_id: String(item.session_id ?? base?.session_id ?? ''),
      session_ord: Number(item.session_ord) || base?.session_ord || prev?.session_ord || 0,
      zh: typeof item.zh === 'string' ? item.zh : base?.zh ?? '', en: typeof item.en === 'string' ? item.en : base?.en ?? '',
      status: String(item.status || base?.status || 'ok'), draft: false, version: version || base?.version || 1,
      tgt_lang: targetLangOf(item, base?.tgt_lang ?? 'en'),
      segments: Array.isArray(item.segments) ? item.segments.map(String) : base?.segments,
      ruby: Array.isArray(item.ruby) ? item.ruby : base?.ruby,
    };
    items.set(item.id, next); trim(); return true;
  }
  function trim() { while (items.size > limit) items.delete(items.keys().next().value as string); }
  function lines(): CaptionLine[] {
    return [...items.values()].sort((a, b) => (a.session_ord - b.session_ord) || (a.seq - b.seq));
  }
  return { apply, lines, items, reset() { items.clear(); deleted.clear(); epochFloor = 0; } };
}

export type TargetPart = { kind: 'text'; text: string } | { kind: 'ruby'; text: string; reading: string } | { kind: 'wbr' };

/** Ruby is drawn only on the first line of a session where a term appears (room_target.js rule). */
export function createRubyMemory() {
  const owner = new Map<string, string>();
  return {
    allow(sessionId: string, text: string, lineId: string) {
      const key = sessionId + '\u0000' + text;
      if (!owner.has(key)) { owner.set(key, lineId); return true; }
      return owner.get(key) === lineId;
    },
    reset() { owner.clear(); },
  };
}

/** Split the target line into text / ruby / <wbr> parts (React renders them; no HTML strings). */
export function targetParts(line: Pick<CaptionLine, 'id' | 'session_id' | 'en' | 'tgt_lang' | 'segments' | 'ruby'>,
  memory?: ReturnType<typeof createRubyMemory>, rubyOn = true): TargetPart[] {
  const text = line.en || '';
  if (!text) return [];
  if (line.tgt_lang !== 'ja') return [{ kind: 'text', text }];
  const segs = Array.isArray(line.segments) && line.segments.join('') === text ? line.segments : [text];
  const pending: RubyTerm[] = [];
  if (rubyOn) for (const r of line.ruby || []) {
    if (!r || typeof r.text !== 'string' || typeof r.reading !== 'string' || !r.text || !r.reading || !text.includes(r.text)) continue;
    if (memory && !memory.allow(line.session_id, r.text, line.id)) continue;
    pending.push(r);
  }
  const parts: TargetPart[] = [];
  segs.forEach((seg, i) => {
    if (i > 0) parts.push({ kind: 'wbr' });
    let rest = String(seg);
    while (rest) {
      let hit: RubyTerm | null = null, at = -1;
      for (const r of pending) { const k = rest.indexOf(r.text); if (k >= 0 && (at < 0 || k < at)) { at = k; hit = r; } }
      if (!hit) { parts.push({ kind: 'text', text: rest }); break; }
      if (at > 0) parts.push({ kind: 'text', text: rest.slice(0, at) });
      parts.push({ kind: 'ruby', text: hit.text, reading: hit.reading });
      pending.splice(pending.indexOf(hit), 1);
      rest = rest.slice(at + hit.text.length);
    }
  });
  return parts;
}

export type GpuStatus = { requested?: string | null; active?: string; ok?: boolean | null; error?: string; device?: string; selftest_ms?: number | null; fell_back_at?: number | null };
/** ASR 加速 chip text + tone from /api/setup asr_gpu (app/asr_gpu.py gpu_status()). */
export function asrChip(gpu: GpuStatus | null | undefined, asrMode?: string): { label: string; tone: 'ready' | 'warn' | 'idle'; detail: string } {
  if (!gpu) return { label: 'ASR 狀態未知', tone: 'idle', detail: '尚未連上 Breeze 引擎' };
  const active = String(gpu.active || 'cpu');
  if (active !== 'cpu' && gpu.ok !== false) return { label: 'ASR 加速 · ' + active.toUpperCase(), tone: 'ready', detail: (gpu.device || 'GPU') + (gpu.selftest_ms ? ' · 自檢 ' + Math.round(gpu.selftest_ms) + ' ms' : '') };
  if (gpu.requested && gpu.requested !== 'cpu') return { label: 'ASR 已退回 CPU', tone: 'warn', detail: (gpu.error || '加速自檢未通過') + '（' + gpu.requested + ' → cpu）' };
  return { label: 'ASR · CPU', tone: 'idle', detail: asrMode ? '模式 ' + asrMode : '未啟用 GPU（BREEZE_ASR_GPU=vulkan 可開啟）' };
}

// ---------------------------------------------------------------- demo data (clearly labelled)
export const DEMO_LABEL = '示範資料（假 ASR／MT，未連線）';
export function demoEvents(lang: TargetLang): BreezeEvent[] {
  const s = 'demo-session';
  if (lang === 'ja') return [
    { type: 'final', id: 'd1', seq: 1, session_id: s, session_ord: 1, zh: '先把注意力帶回來，覺察此刻的呼吸。', en: '注意を戻して、今この瞬間の呼吸に気づきましょう。', tgt_lang: 'ja', segments: ['注意を戻して、', '今この瞬間の', '呼吸に気づきましょう。'], ruby: [{ text: '呼吸', reading: 'こきゅう' }], version: 1 },
    { type: 'final', id: 'd2', seq: 2, session_id: s, session_ord: 2, zh: '禪修不是逃避，而是看清楚。', en: '禅修は逃避ではなく、はっきり見ることです。', tgt_lang: 'ja', segments: ['禅修は逃避ではなく、', 'はっきり見ることです。'], ruby: [{ text: '禅修', reading: 'ぜんしゅう' }, { text: '逃避', reading: 'とうひ' }], version: 1 },
    { type: 'draft', id: 'd3', seq: 3, session_id: s, zh: '接下來我們一起靜坐五分鐘' },
  ];
  return [
    { type: 'final', id: 'd1', seq: 1, session_id: s, session_ord: 1, zh: '先把注意力帶回來，覺察此刻的呼吸。', en: 'Bring your attention back and notice your breathing in this moment.', version: 1 },
    { type: 'final', id: 'd2', seq: 2, session_id: s, session_ord: 2, zh: '禪修不是逃避，而是看清楚。', en: 'Meditation is not an escape; it is seeing clearly.', version: 1 },
    { type: 'draft', id: 'd3', seq: 3, session_id: s, zh: '接下來我們一起靜坐五分鐘' },
  ];
}
export function demoFinalFor(lang: TargetLang): BreezeEvent {
  return lang === 'ja'
    ? { type: 'final', id: 'd3', seq: 3, session_id: 'demo-session', session_ord: 3, zh: '接下來我們一起靜坐五分鐘。', en: 'これから一緒に五分間、静坐しましょう。', tgt_lang: 'ja', segments: ['これから一緒に', '五分間、', '静坐しましょう。'], ruby: [{ text: '静坐', reading: 'せいざ' }], version: 1 }
    : { type: 'final', id: 'd3', seq: 3, session_id: 'demo-session', session_ord: 3, zh: '接下來我們一起靜坐五分鐘。', en: 'Next, let us sit in meditation together for five minutes.', version: 1 };
}
