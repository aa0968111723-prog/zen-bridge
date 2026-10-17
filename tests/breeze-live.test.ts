import assert from 'node:assert/strict';
import { createCaptionStore, createRubyMemory, targetParts, canChangeTarget, asrChip, demoEvents, demoFinalFor, targetLangOf } from '../lib/breeze-live';
import { liveRouteAllowed, adminRouteAllowed } from '../lib/breeze-proxy';
import { GET as adminGet } from '../app/api/breeze-admin/[...path]/route';
import { GET as liveGet } from '../app/api/breeze/[...path]/route';

let checks = 0;
const ok = (fn: () => void | Promise<void>) => { checks++; return fn(); };

// 1. grey draft is replaced in place by the final with the same id
await ok(() => {
  const s = createCaptionStore();
  s.apply({ type: 'final', id: 'a', seq: 1, session_id: 'x', session_ord: 1, zh: '一', en: 'one', version: 1 });
  s.apply({ type: 'draft', id: 'b', seq: 2, session_id: 'x', zh: '二（草稿）' });
  let lines = s.lines();
  assert.equal(lines.length, 2);
  assert.equal(lines[1].draft, true);
  assert.equal(lines[1].session_ord, 1, 'draft sorts after the session finals');
  s.apply({ type: 'final', id: 'b', seq: 2, session_id: 'x', session_ord: 2, zh: '二', en: 'two', version: 1 });
  lines = s.lines();
  assert.equal(lines.length, 2, 'final replaces the draft, not appended');
  assert.deepEqual([lines[1].draft, lines[1].zh, lines[1].en], [false, '二', 'two']);
});
// 2. a late draft never overwrites a final; deletes/clears honoured; stale versions ignored
await ok(() => {
  const s = createCaptionStore();
  s.apply({ type: 'final', id: 'a', seq: 1, zh: '定稿', en: 'final', version: 2 });
  assert.equal(s.apply({ type: 'draft', id: 'a', seq: 1, zh: '舊草稿' }), false);
  assert.equal(s.apply({ type: 'final', id: 'a', seq: 1, zh: '舊版', en: 'old', version: 1 }), false);
  assert.equal(s.lines()[0].zh, '定稿');
  s.apply({ type: 'caption_deleted', id: 'a' });
  assert.equal(s.lines().length, 0);
  assert.equal(s.apply({ type: 'final', id: 'a', seq: 1, zh: 'x', en: 'x' }), false, 'deleted ids stay deleted');
  s.apply({ type: 'final', id: 'c', seq: 3, zh: 'c', en: 'c' });
  s.apply({ type: 'captions_cleared', epoch: 5 });
  assert.equal(s.lines().length, 0);
  assert.equal(s.apply({ type: 'ping' }), false);
});
// 3. one target language per session, locked once started
await ok(() => {
  assert.equal(canChangeTarget(false, 'en', 'ja').ok, true);
  assert.equal(canChangeTarget(true, 'en', 'ja').ok, false);
  assert.match(String(canChangeTarget(true, 'en', 'ja').reason), /鎖定/);
  assert.equal(canChangeTarget(true, 'ja', 'ja').ok, true);
  assert.equal(canChangeTarget(false, 'en', 'fr').ok, false);
  assert.equal(targetLangOf({ tgt_lang: 'ja' }), 'ja');
  assert.equal(targetLangOf({ tgt_lang: 'en,ja' }), 'en', 'never both');
});
// 4. Japanese: <wbr> segments + ruby only on the first line of the session that has the term
await ok(() => {
  const mem = createRubyMemory();
  const a = { id: 'a', session_id: 's', en: '呼吸を整えて、呼吸に気づく。', tgt_lang: 'ja' as const, segments: ['呼吸を整えて、', '呼吸に気づく。'], ruby: [{ text: '呼吸', reading: 'こきゅう' }] };
  const pa = targetParts(a, mem);
  assert.deepEqual(pa.filter(p => p.kind === 'ruby').length, 1, 'ruby once per term');
  assert.equal(pa.filter(p => p.kind === 'wbr').length, 1);
  assert.equal(pa.map(p => p.kind === 'wbr' ? '' : p.text).join(''), a.en, 'text preserved');
  const b = { ...a, id: 'b', en: '呼吸。', segments: ['呼吸。'] };
  assert.equal(targetParts(b, mem).filter(p => p.kind === 'ruby').length, 0, 'later lines get no ruby');
  assert.equal(targetParts(a, mem).filter(p => p.kind === 'ruby').length, 1, 'owner keeps ruby on repaint');
  const en = targetParts({ id: 'e', session_id: 's', en: 'Hello <b>', tgt_lang: 'en' });
  assert.deepEqual(en, [{ kind: 'text', text: 'Hello <b>' }], 'en is plain text, never HTML');
  assert.equal(targetParts({ ...a, id: 'z', segments: ['mismatch'] }).filter(p => p.kind === 'wbr').length, 0, 'bad segments ignored');
});
// 5. ASR 加速 chip
await ok(() => {
  assert.equal(asrChip({ requested: 'vulkan', active: 'vulkan', ok: true, device: 'Radeon' }).tone, 'ready');
  assert.equal(asrChip({ requested: 'vulkan', active: 'cpu', ok: false, error: 'selftest' }).tone, 'warn');
  assert.equal(asrChip({ active: 'cpu' }).tone, 'idle');
  assert.equal(asrChip(null).tone, 'idle');
});
// 6. demo data is consistent (one language, draft -> final)
await ok(() => {
  for (const lang of ['en', 'ja'] as const) {
    const s = createCaptionStore(); demoEvents(lang).forEach(e => s.apply(e));
    assert.ok(s.lines().at(-1)?.draft);
    s.apply(demoFinalFor(lang));
    assert.ok(s.lines().every(l => !l.draft && l.tgt_lang === lang));
  }
});
// 7. proxy allowlists: host API only, admin GET + review writes only, no auth/token/delete paths
await ok(() => {
  assert.ok(liveRouteAllowed('GET', 'setup'));
  assert.ok(liveRouteAllowed('POST', 'push'));
  assert.ok(liveRouteAllowed('POST', 'rooms/open'));
  assert.ok(!liveRouteAllowed('GET', 'host-token'));
  assert.ok(!liveRouteAllowed('POST', 'glossary'));
  assert.ok(!liveRouteAllowed('GET', '../admin'));
  assert.ok(adminRouteAllowed('GET', 'overview'));
  assert.ok(adminRouteAllowed('POST', 'tm/12/approve'));
  assert.ok(!adminRouteAllowed('POST', 'auth/login'));
  assert.ok(!adminRouteAllowed('GET', 'auth/session'));
  assert.ok(!adminRouteAllowed('POST', 'users'));
  assert.ok(!adminRouteAllowed('POST', 'users/1/tokens'));
  assert.ok(!adminRouteAllowed('POST', 'glossary/import'));
  assert.ok(!adminRouteAllowed('DELETE', 'sessions/x'));
  assert.ok(!adminRouteAllowed('GET', 'a/../b'));
});
// 8. routes: admin without token -> 401 needsToken; live offline -> 503; unknown -> 404
await ok(async () => {
  delete process.env.BREEZE_ADMIN_TOKEN;
  const r = await adminGet(new Request('http://zen/api/breeze-admin/overview'), { params: Promise.resolve({ path: ['overview'] }) });
  assert.equal(r.status, 401); assert.equal((await r.json() as { needsToken: boolean }).needsToken, true);
  const r404 = await adminGet(new Request('http://zen/api/breeze-admin/auth/session'), { params: Promise.resolve({ path: ['auth', 'session'] }) });
  assert.equal(r404.status, 404);
  process.env.BREEZE_LIVE_URL = 'http://127.0.0.1:9';
  const off = await liveGet(new Request('http://zen/api/breeze/setup'), { params: Promise.resolve({ path: ['setup'] }) });
  assert.equal(off.status, 503);
  const no = await liveGet(new Request('http://zen/api/breeze/host-token'), { params: Promise.resolve({ path: ['host-token'] }) });
  assert.equal(no.status, 404);
  delete process.env.BREEZE_LIVE_URL;
});
console.log(`Breeze live captions, language lock, Japanese ruby, ASR chip, demo data and proxy allowlists passed (${checks} groups).`);
