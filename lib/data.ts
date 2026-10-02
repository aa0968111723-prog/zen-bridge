import { getBindings, getEnv } from './env';
import type { DatabaseIssue } from './domain';

export type Command = { sql: string; args: unknown[] };
const NOT_READY = '資料庫尚未就緒';
export class DatabaseUnavailableError extends Error {
  constructor(readonly code: DatabaseIssue) { super(NOT_READY); }
}

// Preserve quoted strings/identifiers while translating D1 placeholders. SQLite
// `IS ?` is its null-safe equality operator, not PostgreSQL's boolean IS syntax.
export function postgresSql(sql: string): string {
  let index = 0;
  return sql.replace(/'(?:''|[^'])*'|"(?:""|[^"])*"|\bIS\s*\?|\?/gi, token => {
    if (token.startsWith("'") || token.startsWith('"')) return token;
    const parameter = '$' + (++index);
    return /^IS/i.test(token) ? 'IS NOT DISTINCT FROM ' + parameter : parameter;
  });
}
async function execute<T>(sql: string, args: unknown[]): Promise<T[]> {
  const config = getEnv();
  try {
    if (config.DEPLOY_TARGET === 'cloudflare') {
      const d1 = getBindings().DB;
      if (!d1) throw new DatabaseUnavailableError('D1_BINDING_MISSING');
      return (await d1.prepare(sql).bind(...args).all<T>()).results;
    }
    if (!config.DATABASE_URL) throw new DatabaseUnavailableError('DATABASE_URL_MISSING');
    const { queryPostgres } = await import('@/lib/platform/node');
    return await queryPostgres<T>(config.DATABASE_URL, postgresSql(sql), args);
  } catch (error) {
    // Driver errors may contain connection strings or query values.
    if (error instanceof DatabaseUnavailableError) throw error;
    throw new DatabaseUnavailableError('DATABASE_UNAVAILABLE');
  }
}
export const rows = <T>(sql: string, ...args: unknown[]) => execute<T>(sql, args);
export async function one<T>(sql: string, ...args: unknown[]): Promise<T | null> {
  return (await execute<T>(sql, args))[0] ?? null;
}
export async function write(sql: string, ...args: unknown[]) {
  await execute(sql, args);
}
export async function batch(commands: Command[]) {
  const config = getEnv();
  try {
    if (config.DEPLOY_TARGET === 'cloudflare') {
      const d1 = getBindings().DB;
      if (!d1) throw new DatabaseUnavailableError('D1_BINDING_MISSING');
      await d1.batch(commands.map(c => d1.prepare(c.sql).bind(...c.args)));
    } else {
      if (!config.DATABASE_URL) throw new DatabaseUnavailableError('DATABASE_URL_MISSING');
      const { batchPostgres } = await import('@/lib/platform/node');
      await batchPostgres(config.DATABASE_URL, commands.map(c => ({ ...c, sql: postgresSql(c.sql) })));
    }
  } catch (error) {
    if (error instanceof DatabaseUnavailableError) throw error;
    throw new DatabaseUnavailableError('DATABASE_UNAVAILABLE');
  }
}
// Compatibility for the existing revision transactions; statements remain data.
export function db() {
  return {
    prepare: (sql: string) => ({ bind: (...args: unknown[]): Command => ({ sql, args }) }),
    batch,
  };
}
export type AudioObject = {
  body: BodyInit; size: number; contentType: string; arrayBuffer(): Promise<ArrayBuffer>;
};
export async function putAudio(key: string, bytes: Uint8Array, mime: string): Promise<string | undefined> {
  const config = getEnv();
  if (config.DEPLOY_TARGET === 'cloudflare') {
    const bucket = getBindings().BUCKET;
    if (!bucket) return undefined;
    await bucket.put(key, bytes, { httpMetadata: { contentType: mime } });
  } else {
    if (!config.AUDIO_DIR) return undefined;
    const { putVolumeAudio } = await import('@/lib/platform/node');
    await putVolumeAudio(config.AUDIO_DIR, key, bytes, mime);
  }
  return key;
}
export async function getAudio(key: string): Promise<AudioObject | null> {
  const config = getEnv();
  if (config.DEPLOY_TARGET === 'zeabur') {
    if (!config.AUDIO_DIR) return null;
    const { getVolumeAudio } = await import('@/lib/platform/node');
    return getVolumeAudio(config.AUDIO_DIR, key);
  }
  const object = await getBindings().BUCKET?.get(key);
  return object ? { body: object.body, size: object.size, contentType: object.httpMetadata?.contentType ?? 'audio/wav', arrayBuffer: () => object.arrayBuffer() } : null;
}
export async function deleteAudio(key: string) {
  const config = getEnv();
  if (config.DEPLOY_TARGET === 'cloudflare') await getBindings().BUCKET?.delete(key);
  else if (config.AUDIO_DIR) {
    const { deleteVolumeAudio } = await import('@/lib/platform/node');
    await deleteVolumeAudio(config.AUDIO_DIR, key);
  }
}
