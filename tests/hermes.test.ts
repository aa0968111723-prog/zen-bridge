import assert from 'node:assert/strict';
import { parseEnv, runWithEnv } from '../lib/env';
import { checkHermesConnection, translateWithHermes } from '../lib/hermes';
import { textTranslationProvider, translateUtterance } from '../lib/translation';
import { workspace, mutate } from '../lib/server';
import { GET as checkConnection } from '../app/api/hermes/route';
import { POST as audio } from '../app/api/audio/route';

const secret = crypto.randomUUID(); // Generated fixture; never a platform secret.
const bindings = { HERMES_API_URL: 'https://hermes.example/v1', HERMES_API_KEY: secret };
const config = parseEnv(bindings);
assert.equal(textTranslationProvider(config), 'hermes');
assert.equal(textTranslationProvider(parseEnv({ ...bindings, OPENAI_API_KEY: secret })), 'hermes');
assert.equal(textTranslationProvider(parseEnv({ ...bindings, OPENAI_API_KEY: secret, TRANSLATION_PROVIDER: 'openai' })), 'openai');
assert.equal(textTranslationProvider(parseEnv({ OPENAI_API_KEY: secret })), 'openai');
assert.equal(textTranslationProvider(parseEnv({})), '');
assert.equal(textTranslationProvider(parseEnv({ OPENAI_API_KEY: secret, TRANSLATION_PROVIDER: 'hermes' })), '');
assert.equal(textTranslationProvider(parseEnv({ ...bindings, TRANSLATION_PROVIDER: 'openai' })), '');
for (const raw of [
  { TRANSLATION_PROVIDER: secret },
  { HERMES_API_URL: 'https://user:' + secret + '@hermes.example' },
  { HERMES_API_URL: 'https://hermes.example?key=' + secret },
  { HERMES_API_URL: 'https://hermes.example#' + secret },
  { HERMES_API_URL: 'http://hermes.example' },
  { HERMES_API_KEY: secret + '\n' },
]) assert.throws(() => parseEnv(raw), e => e instanceof Error && !e.message.includes(secret));
assert.doesNotThrow(() => parseEnv({ DEPLOY_TARGET: 'zeabur', HERMES_API_URL: 'http://hermes-agent.zeabur.internal:5000/v1' }));
assert.throws(() => parseEnv({ DEPLOY_TARGET: 'cloudflare', HERMES_API_URL: 'http://hermes-agent.zeabur.internal:5000/v1' }), /HERMES_API_URL/);

const originalFetch = globalThis.fetch;
const requests: { url: string; init?: RequestInit }[] = [];
let answer: unknown = { choices: [{ message: { role: 'assistant', content: '  Be aware.  ', reasoning_content: secret }, finish_reason: 'stop' }] };
let status = 200, failNetwork = false;
globalThis.fetch = async (url, init) => {
  requests.push({ url: String(url), init });
  if (failNetwork) throw new Error(secret);
  if (String(url).endsWith('/models')) return Response.json({ object: 'list', data: [{ id: 'hermes-agent' }] }, { status });
  if (String(url).endsWith('/transcriptions')) return Response.json({ text: '覺察當下。' });
  if (String(url).endsWith('/responses')) return Response.json({ output: [{ content: [{ type: 'output_text', text: 'OpenAI result' }] }] });
  return Response.json(status === 200 ? answer : { error: secret }, { status });
};
try {
  assert.deepEqual(await checkHermesConnection(config), { status: 'connected', model: 'hermes-agent' });
  assert.equal(requests.at(-1)!.url, 'https://hermes.example/v1/models');
  const probe = await runWithEnv(bindings, () => checkConnection());
  assert.equal(probe.status, 200); assert.equal(probe.headers.get('Cache-Control'), 'no-store');
  assert.ok(!JSON.stringify(await probe.json()).includes(secret));
  assert.equal(await translateWithHermes('Translate only.', 'Source text', config), 'Be aware.');
  const request = requests.at(-1)!;
  assert.equal(request.url, 'https://hermes.example/v1/chat/completions');
  assert.equal(request.init!.redirect, 'error');
  assert.equal(new Headers(request.init!.headers).get('Authorization'), 'Bearer ' + secret);
  const body = JSON.parse(request.init!.body as string);
  assert.deepEqual(body.messages, [{ role: 'system', content: 'Translate only.' }, { role: 'user', content: 'Source text' }]);
  assert.equal(body.stream, false);
  assert.ok(!JSON.stringify(body).includes(secret));
  await translateWithHermes('Translate.', 'source', parseEnv({ ...bindings, HERMES_API_URL: 'https://hermes.example/' }));
  assert.equal(requests.at(-1)!.url, 'https://hermes.example/v1/chat/completions');
  assert.equal(await translateUtterance('Translate.', 'source', parseEnv({ OPENAI_API_KEY: secret })), 'OpenAI result');
  const before = requests.length;
  await assert.rejects(translateUtterance('Translate.', 'source', parseEnv({ TRANSLATION_PROVIDER: 'hermes', OPENAI_API_KEY: secret })), /HERMES_API_URL/);
  assert.equal(requests.length, before); // An explicitly missing provider never falls back.
  for (const code of [401, 403, 429, 503]) {
    status = code;
    await assert.rejects(translateUtterance('Translate.', 'source', parseEnv({ ...bindings, OPENAI_API_KEY: secret })), e => e instanceof Error && !e.message.includes(secret));
    assert.ok(requests.at(-1)!.url.endsWith('/chat/completions'));
  }
  status = 200; failNetwork = true;
  await assert.rejects(translateWithHermes('Translate.', 'source', config), /此平台無法連到 Hermes/);
  failNetwork = false;
  for (const invalid of [
    { choices: [{ message: { role: 'assistant', content: '' }, finish_reason: 'stop' }] },
    { choices: [{ message: { role: 'assistant', content: 'partial' }, finish_reason: 'length' }] },
    { choices: [{ message: { role: 'assistant', content: null, tool_calls: [{}] }, finish_reason: 'tool_calls' }] },
    { choices: [] }, null,
  ]) {
    answer = invalid;
    await assert.rejects(translateWithHermes('Translate.', 'source', config));
  }
  answer = { choices: [{ message: { role: 'assistant', content: '覺察當下。' }, finish_reason: 'stop' }] };
  const sid = crypto.randomUUID(), saved: unknown[][] = [];
  const session = { id: sid, title: '社課', topic: '覺察', notes: '課程背景', roles: '{}', speaker_ids: '[]', status: 'ready', created_at: new Date().toISOString() };
  const example = { status: 'verified', zh: '覺察', en: 'awareness', meaning: '留心當下', context: '社課' };
  const history = [{ zh: '第二句', en: 'Second', label: '講者', role: '講者' }, { zh: '第一句', en: 'First', label: '講者', role: '講者' }];
  const queries: string[] = [];
  const db = { prepare(sql: string) { queries.push(sql); return { bind(...args: unknown[]) { return { async all() {
    if (sql.startsWith('INSERT INTO segments')) saved.push(args);
    return { results: sql.includes('FROM sessions') ? [session] : sql.includes('FROM memories') ? [example] : sql.includes('SELECT zh,en,label,role') ? [...history] : [] };
  } }; } }; } } as unknown as D1Database;
  const state = await runWithEnv({ ...bindings, DB: db }, () => workspace());
  assert.equal(state.connection.textTranslation, true);
  assert.equal(state.connection.translationProvider, 'hermes');
  assert.equal(state.connection.speech, false); // A text endpoint cannot enable recording.
  assert.equal(state.connection.hermes, '已設定');
  assert.ok(!JSON.stringify(state).includes(secret));
  await runWithEnv({ ...bindings, DB: db }, () => mutate({ action: 'segment', sessionId: sid, personId: null, role: '講者', direction: 'en-zh', en: 'Be aware.', translate: true }));
  assert.equal(saved.at(-1)![5], 'en-zh');
  assert.equal(saved.at(-1)![6], '覺察當下。');
  assert.equal(saved.at(-1)![7], 'Be aware.');
  const prompt = JSON.parse(JSON.parse(requests.at(-1)!.init!.body as string).messages[1].content);
  assert.equal(prompt.currentUtterance, 'Be aware.');
  assert.equal(prompt.activity.notes, session.notes);
  assert.deepEqual(prompt.confirmedExamples, [example]);
  assert.equal(prompt.previousUtterances[0].zh, '第一句');
  assert.ok(queries.some(sql => sql.includes("status='verified'") && sql.includes('LIMIT 30')));
  assert.ok(queries.some(sql => sql.includes('LIMIT 6')));
  assert.ok(!requests.some(r => r.url.includes('aliyuncs.com')));
  // ASR keeps its original provider. Hermes supplies only the missing translation.
  async function audioRequest() {
    const form = new FormData(); form.set('sessionId', sid);
    form.set('speechProvider', 'openai'); form.set('speechMode', 'chunk');
    form.set('audio', new File([new Uint8Array([1, 2, 3])], 'speech.wav', { type: 'audio/wav' }));
    return runWithEnv({ ...bindings, DB: db, OPENAI_API_KEY: secret, SPEECH_PROVIDER: 'openai' }, () => audio(new Request('http://test/api/audio', { method: 'POST', body: form })));
  }
  answer = { choices: [{ message: { role: 'assistant', content: 'Be aware of the present.' }, finish_reason: 'stop' }] };
  const audioResult = await audioRequest();
  assert.equal(audioResult.status, 200);
  assert.equal((await audioResult.json() as { provider: string }).provider, 'openai');
  assert.equal(saved.at(-1)![7], 'Be aware of the present.');
  status = 503;
  assert.equal((await audioRequest()).status, 200);
  assert.equal(saved.at(-1)![6], '覺察當下。');
  assert.equal(saved.at(-1)![7], '');
  assert.match(saved.at(-1)![10] as string, /Hermes 補譯失敗/);
  assert.ok(!JSON.stringify(saved).includes(secret));
} finally { globalThis.fetch = originalFetch; }
console.log('Hermes text routing, API validation, context, secret handling and audio fallback passed.');
