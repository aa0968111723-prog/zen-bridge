import postgres from 'postgres';
import { readMigrationFiles } from 'drizzle-orm/migrator';
import WebSocket from 'ws';
import { mkdir, readFile, writeFile, rename, rm } from 'node:fs/promises';
import { dirname, join, resolve, sep } from 'node:path';
import type { Command, AudioObject } from '../data';
import type { LiveSocket } from '../qwen-live';

const clients = new Map<string, ReturnType<typeof postgres>>();
const ready = new Map<string, Promise<void>>();
async function database(url: string) {
  let client = clients.get(url);
  if (!client) {
    client = postgres(url, { max: 5, connect_timeout: 10, idle_timeout: 20, onnotice: () => {} });
    clients.set(url, client);
  }
  // Serialize first-use migrations across instances with a PostgreSQL lock.
  let migration = ready.get(url);
  if (!migration) {
    migration = (async () => {
      await client!.begin(async tx => {
        await tx`SELECT pg_advisory_xact_lock(79438201)`;
        const migrations = readMigrationFiles({ migrationsFolder: join(process.cwd(), 'drizzle-pg') });
        await tx`CREATE SCHEMA IF NOT EXISTS drizzle`;
        await tx`CREATE TABLE IF NOT EXISTS drizzle.__drizzle_migrations (id serial PRIMARY KEY, hash text NOT NULL, created_at bigint)`;
        const [last] = await tx`SELECT created_at FROM drizzle.__drizzle_migrations ORDER BY created_at DESC LIMIT 1`;
        for (const migration of migrations) {
          if (last && Number(last.created_at) >= migration.folderMillis) continue;
          for (const sql of migration.sql) await tx.unsafe(sql);
          await tx`INSERT INTO drizzle.__drizzle_migrations (hash, created_at) VALUES (${migration.hash}, ${migration.folderMillis})`;
        }
      });
    })();
    ready.set(url, migration);
    migration.catch(() => ready.delete(url));
  }
  await migration;
  return client;
}
export async function queryPostgres<T>(url: string, sql: string, args: unknown[]): Promise<T[]> {
  const client = await database(url);
  return await client.unsafe(sql, args as postgres.ParameterOrJSON<never>[]) as unknown as T[];
}
export async function batchPostgres(url: string, commands: Command[]) {
  const client = await database(url);
  await client.begin(async tx => {
    for (const command of commands) await tx.unsafe(command.sql, command.args as postgres.ParameterOrJSON<never>[]);
  });
}
function audioPath(directory: string, key: string) {
  // Keys come from UUID-based routes; reject traversal even for database values.
  if (!/^(recordings|references)\/[a-f0-9-]+\/[a-f0-9-]+$/i.test(key)) throw new Error('音檔路徑格式不正確。');
  const root = resolve(directory), path = resolve(root, key);
  if (!path.startsWith(root + sep)) throw new Error('音檔路徑格式不正確。');
  return path;
}
export async function putVolumeAudio(directory: string, key: string, bytes: Uint8Array, mime: string) {
  const path = audioPath(directory, key), temporary = path + '.' + crypto.randomUUID() + '.tmp';
  try {
    await mkdir(dirname(path), { recursive: true });
    await writeFile(temporary, bytes, { mode: 0o600 });
    await rename(temporary, path);
    await writeFile(path + '.json', JSON.stringify({ mime }), { mode: 0o600 });
  } catch {
    await rm(temporary, { force: true }).catch(() => {});
    throw new Error('AUDIO_DIR 音檔儲存尚未就緒。');
  }
}
export async function getVolumeAudio(directory: string, key: string): Promise<AudioObject | null> {
  const path = audioPath(directory, key);
  try {
    const bytes = await readFile(path);
    const metadata = JSON.parse(await readFile(path + '.json', 'utf8')) as { mime: string };
    const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
    return { body: buffer, size: bytes.length, contentType: metadata.mime, arrayBuffer: async () => buffer };
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null;
    throw new Error('AUDIO_DIR 音檔讀取失敗。');
  }
}
export async function deleteVolumeAudio(directory: string, key: string) {
  const path = audioPath(directory, key);
  try { await Promise.all([rm(path, { force: true }), rm(path + '.json', { force: true })]); }
  catch { throw new Error('AUDIO_DIR 音檔刪除失敗。'); }
}
export function openNodeSocket(endpoint: string, apiKey: string): Promise<LiveSocket> {
  return new Promise((resolveSocket, reject) => {
    const socket = new WebSocket(endpoint.replace(/^https:/, 'wss:'), { headers: { Authorization: 'Bearer ' + apiKey }, handshakeTimeout: 15000 });
    const fail = () => { socket.terminate(); reject(new Error('Zeabur 無法連線至阿里雲。請檢查平台網路與 DASHSCOPE_API_KEY。')); };
    socket.once('error', fail);
    socket.once('open', () => {
      socket.removeListener('error', fail);
      // Keep an EventEmitter error listener before protocol listeners attach.
      socket.on('error', () => {});
      resolveSocket(socket as unknown as LiveSocket);
    });
  });
}
