import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createServer } from 'node:net';
import { once } from 'node:events';

// Run after DEPLOY_TARGET=zeabur pnpm build. No platform secrets are needed.
const cleanEnv = { ...process.env };
for (const name of ['DEPLOY_TARGET', 'DATABASE_URL', 'DASHSCOPE_API_KEY', 'OPENAI_API_KEY', 'AUDIO_DIR']) delete cleanEnv[name];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function inferredTarget(variables, zeaburEntry = false) {
  const result = spawnSync(process.execPath, ['--experimental-strip-types', '--input-type=module', '-e',
    "const {getEnv,initializeNodeRuntime}=await import('./lib/env.ts');" +
    (zeaburEntry ? "initializeNodeRuntime('zeabur');" : '') + "console.log(getEnv().DEPLOY_TARGET)"],
    { env: { ...cleanEnv, ...variables }, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}
assert.equal(inferredTarget({}), 'cloudflare');
assert.equal(inferredTarget({ DATABASE_URL: 'present' }), 'zeabur');
assert.equal(inferredTarget({ DATABASE_URL: 'present', DEPLOY_TARGET: 'cloudflare' }), 'cloudflare');
assert.equal(inferredTarget({}, true), 'zeabur');

for (const target of ['', crypto.randomUUID(), 'cloudflare']) {
  const result = spawnSync(process.execPath, ['--experimental-strip-types', 'scripts/start.mjs', '--zeabur'],
    { env: { ...cleanEnv, DEPLOY_TARGET: target }, encoding: 'utf8' });
  assert.notEqual(result.status, 0);
  assert.ok(result.stderr.includes('DEPLOY_TARGET'));
  if (target && target !== 'cloudflare') assert.ok(!result.stderr.includes(target));
}

async function verifyStartup(args, variables) {
  const reservation = createServer();
  reservation.listen(0, '127.0.0.1');
  await once(reservation, 'listening');
  const port = reservation.address().port;
  await new Promise((resolve, reject) => reservation.close(error => error ? reject(error) : resolve()));
  const child = spawn(process.execPath, ['--experimental-strip-types', 'scripts/start.mjs', ...args],
    { env: { ...cleanEnv, ...variables, PORT: String(port) }, stdio: ['ignore', 'pipe', 'pipe'] });
  let log = '';
  child.stdout.on('data', bytes => { log += bytes; });
  child.stderr.on('data', bytes => { log += bytes; });
  try {
    let html;
    for (let attempt = 0; attempt < 100; attempt++) {
      if (child.exitCode !== null) throw new Error('Runtime exited before listening: ' + log);
      try { html = await fetch('http://127.0.0.1:' + port + '/'); break; }
      catch { await delay(100); }
    }
    assert.ok(html, 'Runtime did not listen: ' + log);
    assert.equal(html.status, 200);
    assert.ok((await html.text()).includes('禪譯'));
    assert.ok(log.includes('0.0.0.0:' + port));
    const api = await fetch('http://127.0.0.1:' + port + '/api/workspace');
    assert.equal(api.status, 503);
    assert.equal((await api.json()).error, '資料庫尚未就緒');
    assert.equal(child.exitCode, null);
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      const stopped = once(child, 'exit');
      child.kill('SIGTERM');
      await stopped;
    }
  }
}
// The production failure: Zeabur's named command, with neither deployment
// target nor database variables present in the runtime container.
await verifyStartup(['--zeabur'], {});
await verifyStartup([], { DEPLOY_TARGET: 'zeabur' });
console.log('PASS: Zeabur entry point without DATABASE_URL/DEPLOY_TARGET; PORT binding, HTML, database API fallback and explicit-target validation.');
