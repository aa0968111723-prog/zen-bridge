// Local (no-cloud) mode: SQLite in the user data dir + audio files, no bindings, no DATABASE_URL.
import assert from 'node:assert/strict';
import { mkdtemp, rm, readdir, stat } from 'node:fs/promises';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { join,resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { parseEnv, runWithEnv } from '../lib/env';
import { rows, one, write, batch, putAudio, getAudio, deleteAudio, DatabaseUnavailableError } from '../lib/data';
import { localDataDir, localDbPath, localAudioDir, localDatabase, sqliteMigrations, closeLocalDatabases, queryLocal } from '../lib/platform/local';
import { workspace, mutate } from '../lib/server';
import { localPort, localHost, DEFAULT_LOCAL_PORT } from '../scripts/local-config.mjs';
import { copyTables, d1DumpReader, TABLES } from '../scripts/migrate-to-local.mjs';

// Data dir resolution (Windows path matches the installer's %LOCALAPPDATA%\ZenBridge).
assert.equal(localDataDir({ LOCALAPPDATA: 'C:\\Users\\MacMiRyzen5\\AppData\\Local' }, 'win32'), join('C:\\Users\\MacMiRyzen5\\AppData\\Local', 'ZenBridge'));
assert.equal(localDataDir({ XDG_DATA_HOME: '/x/share' }, 'linux'), join('/x/share', 'ZenBridge'));
assert.ok(localDataDir({}, 'linux').endsWith(join('.local', 'share', 'ZenBridge')));
assert.equal(localDataDir({ ZEN_UI_DATA_DIR: '/data/zen', LOCALAPPDATA: 'C:\\x' }, 'win32'), resolve('/data/zen'));
assert.ok(localDbPath('/d').endsWith('zen-ui.sqlite3'));
assert.equal(localAudioDir('/d'), join('/d', 'audio'));

// Binding: loopback by default, never 8645, never the Breeze ports.
assert.equal(localPort({}), DEFAULT_LOCAL_PORT);
assert.equal(localHost({}), '127.0.0.1');
assert.equal(localPort({ ZEN_UI_PORT: '9100' }), 9100);
for (const bad of ['8645', '8780', '8791', '80', 'x', '70000']) assert.throws(() => localPort({ ZEN_UI_PORT: bad }), /ZEN_UI_PORT|保留/);
assert.equal(localHost({ ZEN_UI_HOST: '0.0.0.0' }), '0.0.0.0');
assert.equal(localHost({ ZEN_UI_HOST: '192.168.1.20' }), '192.168.1.20');
assert.throws(() => localHost({ ZEN_UI_HOST: 'example.com' }), /ZEN_UI_HOST/);

// Default target with no cloud configuration.
assert.equal(parseEnv({}).DEPLOY_TARGET, 'local');
assert.equal(parseEnv({ DEPLOY_TARGET: 'local', DATABASE_URL: 'present' }).DEPLOY_TARGET, 'local');

const dir = await mkdtemp(join(tmpdir(), 'zen-local-'));
const env = { DEPLOY_TARGET: 'local', ZEN_UI_DATA_DIR: dir } as unknown as Cloudflare.Env;
const local = <T>(work: () => Promise<T>) => runWithEnv(env, work);
try {
  // Same migrations as D1; applied once, recorded, idempotent on reopen.
  const names = sqliteMigrations().map(m => m.name);
  assert.ok(names.length >= 4 && names[0].startsWith('0000_') && names.includes('0003_dictionary.sql'));
  assert.deepEqual(await local(() => rows<{ name: string }>('SELECT name FROM __zen_migrations ORDER BY name')).then(r => r.map(x => x.name)), names);
  assert.ok(existsSync(join(dir, 'zen-ui.sqlite3')));
  closeLocalDatabases();
  assert.equal(queryLocal<{ n: number }>(localDbPath(dir), 'SELECT count(*) AS n FROM __zen_migrations', [])[0].n, names.length);
  const tables = (await local(() => rows<{ name: string }>("SELECT name FROM sqlite_master WHERE type='table'"))).map(t => t.name);
  for (const t of ['people', 'sessions', 'segments', 'memories', 'memory_revisions', 'segment_revisions', 'dictionary_sources', 'dictionary_entries', 'dictionary_keys']) assert.ok(tables.includes(t), t);

  // Domain flows through lib/server on SQLite (D1 SQL incl. `IS ?` and quoted "offset").
  const empty = await local(() => workspace());
  assert.equal(empty.sessions.length, 0); assert.equal(empty.connection.deployTarget, 'local');
  const sessionId = (await local(() => mutate({ action: 'session', title: '本機社課', topic: '完全本機' })))!.id;
  const personId = (await local(() => mutate({ action: 'person', name: '龜龜', role: '主持人' })))!.id;
  const segmentId = (await local(() => mutate({ action: 'segment', sessionId, personId, role: '主持人', zh: '歡迎', en: 'Welcome' })))!.id;
  await local(() => mutate({ action: 'correct', id: segmentId, personId, role: '主持人', zh: '歡迎', en: 'Welcome, everyone', note: '開場', remember: true }));
  const memoryDupe = { action: 'memory', personId: null, role: '通用', zh: '社課', en: 'club session', meaning: '', context: '' };
  await local(() => mutate(memoryDupe));
  const state = await local(() => workspace(sessionId));
  assert.equal(state.segments[0].en, 'Welcome, everyone');
  assert.equal(state.memories.filter(m => m.status === 'verified').length, 1);
  assert.equal((await local(() => rows('SELECT id FROM segment_revisions WHERE segment_id=?', segmentId))).length, 1);
  assert.ok(await local(() => one('SELECT id FROM memories WHERE person_id IS ? AND zh=?', null, '社課')));

  // Batch is one transaction: a failing statement rolls the whole batch back.
  await assert.rejects(local(() => batch([
    { sql: "INSERT INTO people(id,name,role,notes,created_at) VALUES('p-rollback','x','y','',?)", args: [new Date().toISOString()] },
    { sql: 'INSERT INTO no_such_table VALUES(1)', args: [] },
  ])), (e: unknown) => e instanceof DatabaseUnavailableError && e.code === 'DATABASE_UNAVAILABLE');
  assert.equal(await local(() => one("SELECT id FROM people WHERE id='p-rollback'")), null);
  await local(() => write('UPDATE sessions SET status=? WHERE id=?', 'finished', sessionId));
  assert.equal((await local(() => one<{ status: string }>('SELECT status FROM sessions WHERE id=?', sessionId)))?.status, 'finished');

  // Audio goes to <data dir>/audio without BUCKET or AUDIO_DIR.
  const key = 'references/' + personId + '/' + crypto.randomUUID();
  assert.equal(await local(() => putAudio(key, new Uint8Array([9, 8, 7]), 'audio/wav')), key);
  assert.ok((await stat(join(dir, 'audio', key))).isFile());
  const object = await local(() => getAudio(key));
  assert.equal(object?.contentType, 'audio/wav');
  assert.deepEqual(new Uint8Array(await object!.arrayBuffer()), new Uint8Array([9, 8, 7]));
  await assert.rejects(local(() => getAudio('../zen-ui.sqlite3')), /路徑/);
  await local(() => deleteAudio(key));
  assert.equal(await local(() => getAudio(key)), null);
  // AUDIO_DIR still overrides the audio folder in local mode.
  const override = join(dir, 'override');
  await runWithEnv({ ...env, AUDIO_DIR: override } as unknown as Cloudflare.Env, () => putAudio(key, new Uint8Array([1]), 'audio/wav'));
  assert.deepEqual(await readdir(join(override, 'references', personId)), [key.split('/').pop(), key.split('/').pop() + '.json'].sort());

  // A broken migration is rolled back and not recorded.
  const badFolder = join(dir, 'bad-migrations'); mkdirSync(badFolder);
  writeFileSync(join(badFolder, '0000_ok.sql'), 'CREATE TABLE a(x int);\n--> statement-breakpoint\nCREATE TABLE b(y int);');
  writeFileSync(join(badFolder, '0001_bad.sql'), 'CREATE TABLE c(z int);\n--> statement-breakpoint\nTHIS IS NOT SQL;');
  assert.throws(() => localDatabase(join(dir, 'bad.sqlite3'), badFolder));
  const okFolder = join(dir, 'ok-migrations'); mkdirSync(okFolder);
  writeFileSync(join(okFolder, '0000_ok.sql'), 'CREATE TABLE a(x int);');
  const reopened = localDatabase(join(dir, 'bad.sqlite3'), okFolder);
  assert.deepEqual((reopened.prepare('SELECT name FROM __zen_migrations').all() as { name: string }[]).map(r => r.name), ['0000_ok.sql']);
  assert.equal(reopened.prepare("SELECT name FROM sqlite_master WHERE name='c'").all().length, 0);

  // Cloud → local migration: a D1 export (SQL dump) lands in a fresh local file; re-running is idempotent.
  const dump = sqliteMigrations().flatMap(m => m.statements).join(';\n') + `;
    INSERT INTO people(id,name,role,notes,created_at) VALUES('p1','雲端講者','主持人','','2026-10-01T00:00:00Z');
    INSERT INTO sessions(id,title,topic,notes,created_at) VALUES('s1','雲端社課','','','2026-10-01T00:00:00Z');
    INSERT INTO segments(id,session_id,person_id,label,role,zh,en,"offset",source,created_at) VALUES('g1','s1','p1','雲端講者','主持人','你好','Hello',1.5,'manual','2026-10-01T00:00:01Z');`;
  const target = join(dir, 'migrated', 'zen-ui.sqlite3');
  const first = await copyTables(d1DumpReader(dump), target);
  assert.deepEqual(Object.keys(first), TABLES);
  assert.deepEqual([first.people.inserted, first.sessions.inserted, first.segments.inserted], [1, 1, 1]);
  const again = await copyTables(d1DumpReader(dump), target);
  assert.equal(again.segments.inserted, 0);
  assert.equal(queryLocal<{ en: string; offset: number }>(target, 'SELECT en,"offset" FROM segments', [])[0].offset, 1.5);
} finally {
  closeLocalDatabases();
  await rm(dir, { recursive: true, force: true });
}
console.log('PASS: local SQLite/file storage — data dir, migrations, domain flows, batch rollback, audio, port/host guard, cloud→local migration.');
