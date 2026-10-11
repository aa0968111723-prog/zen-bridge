// 示範資料（假資料）for the 管理後台 tab when the admin backend is not reachable. Labelled in UI.
const now = Date.now() / 1000;
export const ADMIN_DEMO: Record<string, unknown> = {
  overview: { rtf: 0.42, rtf_color: 'green', stale: false, live_age_s: 2, asr_gpu: { requested: 'vulkan', active: 'vulkan', ok: true, device: 'AMD Radeon (Vega 7)', selftest_ms: 412 },
    components: { live_room: 'up', admin_db: 'ok', worker: 'idle', mt: 'up', backup: 'ok' }, live: { listeners: 12, queue_depth: 0, sessions_active: 1, rtf: 0.42 } },
  'overview/inventory': { counts: { sessions: 18, segments: 2364, glossary_en: 412, glossary_ja: 138, glossary_proposed: 6, corrections: 57, jobs: 9 }, backups: { count: 7, last: 'zen-20261011-0300.db', last_age_s: 19620, manifest_present: true } },
  'overview/asr-timings': { count: 120, p50_ms: 980, p90_ms: 1640, p99_ms: 2410, rtf_p50: 0.38, rtf_p90: 0.55 },
  'perf/metrics': { rtf: 0.42, asr_p50_ms: 980, asr_p90_ms: 1640, mt_p50_ms: 310, mt_p90_ms: 620, end_to_end_p50_ms: 1850, cpu_pct: 46, ram_gb: 5.8, gpu: 'vulkan' },
  sessions: { items: [
    { id: 'zen-20261010-1930', room_id: 'class', title: '週六禪修・呼吸', status: 'ended', segments: 214, started_at: now - 50000 },
    { id: 'zen-20261008-1900', room_id: 'class', title: '心經導讀（日文場）', status: 'ended', segments: 188, started_at: now - 220000 },
    { id: 'zen-20261011-0800', room_id: 'class', title: '早課測試', status: 'live', segments: 12, started_at: now - 600 } ] },
  'review/queue': { tm_pending: [{ id: 31, src_text: '覺察呼吸', tgt_text: 'notice the breath', tgt_lang: 'en' }], staging_pending: [{ id: 'st-7', kind: 'correction', tgt_lang: 'ja', src_text: '禪修', tgt_text: '禅修（ぜんしゅう）' }] },
  staging: { items: [{ id: 'st-7', kind: 'correction', state: 'pending', tgt_lang: 'ja', src_text: '禪修', tgt_text: '禅修' }, { id: 'st-6', kind: 'glossary', state: 'approved', tgt_lang: 'en', src_text: '止觀', tgt_text: 'calming and insight' }] },
  'glossary/terms': { items: [{ id: 1, src: '正念', en: 'mindfulness', ja: 'マインドフルネス', status: 'active' }, { id: 2, src: '禪修', en: 'meditation practice', ja: '禅修', status: 'active' }, { id: 3, src: '止觀', en: 'calming and insight', ja: '止観', status: 'proposed' }] },
  tm: { items: [{ id: 31, src_text: '覺察呼吸', tgt_text: 'notice the breath', tgt_lang: 'en', quality: 3 }, { id: 30, src_text: '回到當下', tgt_text: '今ここに戻る', tgt_lang: 'ja', quality: 3 }] },
  search: { items: [{ session_id: 'zen-20261010-1930', seq: 12, text: '先把注意力帶回來，覺察此刻的呼吸。' }], mode: 'fts' },
  exports: { items: [{ name: 'zen-20261010-1930.srt', bytes: 18342, created_at: now - 40000 }, { name: 'zen-20261008-1900.vtt', bytes: 16120, created_at: now - 210000 }] },
  config: { translate_profile: 'local-breeze', default_tgt_lang: 'en', asr: 'native-gpu', BREEZE_ASR_GPU: 'vulkan', retention_days: 180 },
  audit: { items: [{ ts: now - 300, actor: 'owner', kind: 'audit.login', detail: '登入' }, { ts: now - 900, actor: 'owner', kind: 'audit.glossary.approve', detail: '正念 → mindfulness' }] },
  users: { items: [{ id: 1, name: '柏能', role: 'owner', disabled: 0 }, { id: 2, name: '社課助教', role: 'editor', disabled: 0 }] },
};
