import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { WebSocketServer } from 'ws';
import { parseEnv, runWithEnv, getEnv, QWEN_MODEL } from '../lib/env';
import { resolveSpeech } from '../lib/speech/resolve';
import { glossary } from '../lib/speech/glossary';
import { postgresSql, rows, write, db, putAudio, getAudio, deleteAudio } from '../lib/data';
import { qwenEndpoint, qwenLiveTranslate } from '../lib/qwen-live';
import { openNodeSocket } from '../lib/platform/node';
import { workspace, mutate, saveSegment } from '../lib/server';
import { POST } from '../app/api/audio/route';

const secret = crypto.randomUUID(); // Synthetic, generated at runtime; never a platform credential.
assert.equal(parseEnv({}).DEPLOY_TARGET, 'cloudflare');
assert.equal(parseEnv({ DATABASE_URL: 'present' }).DEPLOY_TARGET, 'zeabur');
assert.equal(parseEnv({ DATABASE_URL: 'present', DEPLOY_TARGET: 'cloudflare' }).DEPLOY_TARGET, 'cloudflare');
for (const invalid of ['', 'invalid-' + secret]) {
  assert.throws(() => parseEnv({ DEPLOY_TARGET: invalid }), error => error instanceof Error && error.message.includes('DEPLOY_TARGET') && !error.message.includes(secret));
}
for (const [value, expected] of [[undefined, 200], ['0', 200], ['-1', 200], ['1001', 1000], ['12.9', 12], ['bad', 200]] as const) assert.equal(parseEnv({ HOTWORD_LIMIT: value }).HOTWORD_LIMIT, expected);
for (const [settings, expected] of [
  [{ DASHSCOPE_API_KEY: secret, SPEECH_PROVIDER: 'qwen-live' }, 'qwen-live'],
  [{ OPENAI_API_KEY: secret, SPEECH_PROVIDER: 'openai' }, 'openai'],
  [{ BREEZE_ASR_URL: 'http://127.0.0.1:8770/transcribe' }, 'breeze'],
  [{ DASHSCOPE_API_KEY: secret, OPENAI_API_KEY: secret, SPEECH_PROVIDER: 'openai' }, 'openai'],
] as const) assert.equal(resolveSpeech(parseEnv(settings)).name, expected);
assert.throws(() => resolveSpeech(parseEnv({})), { message: '語音服務尚未連接。請設定所選服務需要的連線資訊。' });
assert.throws(() => parseEnv({ SPEECH_PROVIDER: 'invalid-' + secret }), /SPEECH_PROVIDER/);
const examples = [
  { status: 'candidate' as const, zh: '候選', en: 'candidate' },
  { status: 'archived' as const, zh: '封存', en: 'archived' },
  { status: 'verified' as const, zh: '字'.repeat(41), en: 'long source' },
  { status: 'verified' as const, zh: '太長', en: 'e'.repeat(81) },
  { status: 'verified' as const, zh: '覺察', en: 'awareness' },
  { status: 'verified' as const, zh: '靜坐', en: 'meditation' },
];
assert.deepEqual({ ...glossary(examples, 'zh-en').phrases }, { 覺察: 'awareness', 靜坐: 'meditation' });
assert.equal(glossary(examples, 'zh-en', 1).note, '熱詞 1 條，不是模型微調');
assert.equal(glossary([examples[4]], 'en-zh', 1).phrases['awareness'], '覺察');
assert.equal(postgresSql("SELECT '?' AS value, \"offset\" FROM memories WHERE zh=? AND person_id IS ?"), "SELECT '?' AS value, \"offset\" FROM memories WHERE zh=$1 AND person_id IS NOT DISTINCT FROM $2");
assert.ok(qwenEndpoint().includes('dashscope-intl.aliyuncs.com'));
assert.ok(qwenEndpoint('cn').includes('https://dashscope.aliyuncs.com/'));
assert.ok(qwenEndpoint().endsWith(QWEN_MODEL));
await assert.rejects(runWithEnv({ DEPLOY_TARGET: 'zeabur' }, () => rows('SELECT 1')), { message: '資料庫尚未就緒', code: 'DATABASE_URL_MISSING' });
await assert.rejects(runWithEnv({ DEPLOY_TARGET: 'cloudflare' }, () => rows('SELECT 1')), { message: '資料庫尚未就緒', code: 'D1_BINDING_MISSING' });
const brokenD1 = { prepare: () => { throw new Error(secret); } } as unknown as D1Database;
await assert.rejects(runWithEnv({ DB: brokenD1 }, () => rows('SELECT 1')), { message: '資料庫尚未就緒', code: 'DATABASE_UNAVAILABLE' });
assert.equal(await runWithEnv({ DEPLOY_TARGET: 'zeabur' }, () => putAudio('recordings/a/b', new Uint8Array([1]), 'audio/wav')), undefined);
const volume = await mkdtemp(join(tmpdir(), 'zen-audio-'));
try {
  await runWithEnv({ DEPLOY_TARGET: 'zeabur', AUDIO_DIR: volume }, async () => {
    const key = 'recordings/' + crypto.randomUUID() + '/' + crypto.randomUUID();
    assert.equal(await putAudio(key, new Uint8Array([1, 2, 3]), 'audio/wav'), key);
    const object = await getAudio(key); assert.equal(object?.contentType, 'audio/wav');
    assert.deepEqual(new Uint8Array(await object!.arrayBuffer()), new Uint8Array([1, 2, 3]));
    await assert.rejects(getAudio('../private'), /路徑/);
    await deleteAudio(key); assert.equal(await getAudio(key), null);
  });
} finally { await rm(volume, { recursive: true, force: true }); }
// Request-scoped bindings cannot leak across simultaneous requests.
await Promise.all(['cloudflare', 'zeabur'].map(target => runWithEnv({ DEPLOY_TARGET: target }, async () => {
  await Promise.resolve(); assert.equal(getEnv().DEPLOY_TARGET, target);
})));

function wav(seconds = 9) {
  const bytes = new Uint8Array(44 + seconds * 16000 * 2), view = new DataView(bytes.buffer);
  for (const [offset, text] of [[0, 'RIFF'], [8, 'WAVE'], [12, 'fmt '], [36, 'data']] as const) bytes.set(new TextEncoder().encode(text), offset);
  view.setUint32(4, bytes.length - 8, true); view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, 16000, true);
  view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true); view.setUint32(40, bytes.length - 44, true);
  return bytes;
}
const originalFetch = globalThis.fetch;
let translation = 'Let us be aware.', source = '一起覺察。', fallbackFails = false, translateCalls = 0;
let upgradeCount = 0;
function fakeSocket() {
  const listeners: Record<string, ((e: { data?: string; code?: number }) => void)[]> = {};
  return {
    accept() {}, close() {},
    addEventListener(type: string, callback: (e: { data?: string; code?: number }) => void) { (listeners[type] ??= []).push(callback); },
    send(data: string) {
      const event = JSON.parse(data);
      if (event.type === 'session.finish') queueMicrotask(() => {
        for (const message of [
          { type: 'conversation.item.input_audio_transcription.completed', transcript: source },
          { type: 'response.text.done', text: translation }, { type: 'session.finished' },
        ]) for (const listener of listeners.message ?? []) listener({ data: JSON.stringify(message) });
      });
    },
  };
}
globalThis.fetch = async (input, init) => {
  const url = String(input);
  if (url.includes('aliyuncs.com')) { upgradeCount++; return { webSocket: fakeSocket() } as unknown as Response; }
  if (url.endsWith('/responses')) { translateCalls++; if (fallbackFails) return new Response('', { status: 503 }); return Response.json({ output: [{ content: [{ type: 'output_text', text: 'Fallback English' }] }] }); }
  if (url.endsWith('/transcriptions')) {
    const form = init!.body as FormData;
    assert.equal(form.get('response_format'), 'json');
    return Response.json({ text: '一起覺察。' });
  }
  throw new Error('Unexpected test request');
};
try {
  const live = await qwenLiveTranslate(wav(9), { apiKey: secret, platform: 'cloudflare', direction: 'zh-en', phrases: {} });
  assert.equal(live.source, source); assert.equal(live.translation, translation);
  const bad = wav(); new DataView(bad.buffer).setUint32(40, bad.length, true);
  await assert.rejects(qwenLiveTranslate(bad, { apiKey: secret, platform: 'cloudflare', direction: 'zh-en', phrases: {} }), /WAV/);
  const qwen = await resolveSpeech(parseEnv({ DASHSCOPE_API_KEY: secret, SPEECH_PROVIDER: 'qwen-live' })).transcribe({ audio: wav(), filename: 'speech.wav', mime: 'audio/wav', direction: 'zh-en', phrases: {}, topic: '', speakerNote: '', auto: true, speakerKey: crypto.randomUUID() });
  assert.equal(qwen[0].speakerKey, null); assert.match(qwen[0].note, /不套用講者聲紋/);
  const openai = await resolveSpeech(parseEnv({ OPENAI_API_KEY: secret, SPEECH_PROVIDER: 'openai' })).transcribe({ audio: wav(), filename: 'speech.wav', mime: 'audio/wav', direction: 'zh-en', phrases: {}, topic: '', speakerNote: '', auto: false });
  assert.equal(openai[0].translation, '');

  const sid = crypto.randomUUID(), saved: unknown[][] = [];
  const session = { id: sid, title: 'Test', topic: '', notes: '', roles: '{}', speaker_ids: '[]', status: 'ready', created_at: new Date().toISOString() };
  const d1 = { prepare(sql: string) { return { bind(...args: unknown[]) { return { async all() {
    if (sql.startsWith('INSERT INTO segments')) saved.push(args);
    return { results: sql.includes('FROM sessions') ? [session] : sql.includes('FROM memories') ? [examples[4]] : [] };
  } }; } }; } } as unknown as D1Database;
  async function audioRequest(bindings: Cloudflare.Env) {
    const form = new FormData(); form.set('sessionId', sid); form.set('auto', 'true');
    form.set('speechMode', 'chunk');
    form.set('audio', new File([wav()], 'speech.wav', { type: 'audio/wav' }));
    return runWithEnv({ ...bindings, DB: d1, DEPLOY_TARGET: 'cloudflare' }, () => POST(new Request('http://test/api/audio', { method: 'POST', body: form })));
  }
  assert.equal((await runWithEnv({ DB: d1, DASHSCOPE_API_KEY: secret, SPEECH_PROVIDER: 'qwen-live' }, () => workspace())).connection.speech, true);
  const state = await runWithEnv({ DB: d1, DASHSCOPE_API_KEY: secret, SPEECH_PROVIDER: 'qwen-live' }, () => workspace());
  assert.equal(state.connection.provider, 'qwen-live'); assert.equal(state.connection.asr, QWEN_MODEL);
  assert.ok(!JSON.stringify(state).includes(secret));
  assert.equal((await (await audioRequest({ DASHSCOPE_API_KEY: secret, SPEECH_PROVIDER: 'qwen-live' })).json() as {provider:string}).provider, 'qwen-live');
  assert.equal(saved.at(-1)![2], null); assert.equal(saved.at(-1)![11], null); // no voiceprint, no R2 => empty audio key
  translation = '';
  await audioRequest({ DASHSCOPE_API_KEY: secret, SPEECH_PROVIDER: 'qwen-live' }); assert.equal(translateCalls, 0);
  await audioRequest({ DASHSCOPE_API_KEY: secret, OPENAI_API_KEY: secret, SPEECH_PROVIDER: 'qwen-live' }); assert.equal(saved.at(-1)![7], 'Fallback English');
  fallbackFails = true;
  assert.equal((await audioRequest({ DASHSCOPE_API_KEY: secret, OPENAI_API_KEY: secret, SPEECH_PROVIDER: 'qwen-live' })).status, 200);
  assert.match(saved.at(-1)![10] as string, /補譯失敗/);
  source = '';
  assert.equal((await (await audioRequest({ DASHSCOPE_API_KEY: secret, SPEECH_PROVIDER: 'qwen-live' })).json() as {error:string}).error, '這一段沒有辨識出文字');
  // Manual translation goes over HTTP and never starts a Qwen socket.
  fallbackFails = false;
  const before = upgradeCount;
  await runWithEnv({ DB: d1, OPENAI_API_KEY: secret, DASHSCOPE_API_KEY: secret }, () => mutate({ action: 'segment', sessionId: sid, personId: null, role: '講者', zh: '你好', translate: true }));
  assert.equal(upgradeCount, before);
} finally { globalThis.fetch = originalFetch; }

// Use a real Node WebSocket handshake; API credentials are generated fixtures.
const websocket = new WebSocketServer({ host: '127.0.0.1', port: 0 });
await new Promise<void>(resolve => websocket.once('listening', resolve));
websocket.on('connection', (socket, request) => {
  assert.equal(request.headers.authorization, 'Bearer ' + secret);
  socket.on('message', () => socket.send('received'));
});
const address = websocket.address(); assert.ok(address && typeof address !== 'string');
const socket = await openNodeSocket('ws://127.0.0.1:' + address.port, secret);
await new Promise<void>(resolve => { socket.addEventListener('message', event => { assert.equal(event.data, 'received'); resolve(); }); socket.send('audio'); });
socket.close(); await new Promise<void>(resolve => websocket.close(() => resolve()));

const config = getEnv();
if (config.DEPLOY_TARGET === 'zeabur' && config.DATABASE_URL) {
  const activity = await mutate({ action: 'session', title: 'Postgres validation' });
  assert.ok(activity?.id);
  const key = await saveSegment({ sessionId: activity.id, personId: null, label: '測試', role: '講者', zh: '覺察', en: 'awareness', source: 'test', offset: 8 });
  await mutate({ action: 'correct', id: key, personId: null, role: '講者', zh: '覺察當下', en: 'Be aware of the present', note: '描述覺察', remember: true });
  const snapshot = await workspace(activity.id);
  assert.equal(snapshot.segments[0].offset, 8); assert.equal(snapshot.memories.find(m => m.source_id === key)?.status, 'verified');
  await mutate({ action: 'memory', personId: null, role: '通用', zh: '測試去重', en: 'Duplicate test', context: '測試' });
  await mutate({ action: 'memory', personId: null, role: '通用', zh: '測試去重', en: 'Duplicate test', context: '測試' });
  assert.equal((await rows("SELECT * FROM memories WHERE zh=?", '測試去重')).length, 1);
  const rolledBack = crypto.randomUUID();
  await assert.rejects(db().batch([
    db().prepare('INSERT INTO people(id,name,role,created_at) VALUES(?,?,?,?)').bind(rolledBack, 'Rollback', '講者', new Date().toISOString()),
    db().prepare('INSERT INTO segments(id,session_id,label,role,zh,source,created_at) VALUES(?,?,?,?,?,?,?)').bind(crypto.randomUUID(), crypto.randomUUID(), 'Test', '講者', '', 'test', new Date().toISOString()),
  ]), /資料庫尚未就緒/);
  assert.equal((await rows('SELECT * FROM people WHERE id=?', rolledBack)).length, 0);
  await write("DELETE FROM memories WHERE zh=?", '測試去重');
  console.log('Postgres migration, save, corrections, null-safe deduplication and rollback passed.');
}
console.log('Runtime, provider, glossary, protocol, audio fallback and Node WebSocket checks passed.');
// Close idle Postgres connections without retaining a validation process.
process.exit(0);
