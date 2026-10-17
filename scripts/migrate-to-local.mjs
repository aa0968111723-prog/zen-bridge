// One-time migration of existing cloud data into the local SQLite file (run once, explicitly).
//   node --experimental-strip-types scripts/migrate-to-local.mjs --from-postgres          (uses DATABASE_URL; Zeabur)
//   node --experimental-strip-types scripts/migrate-to-local.mjs --from-d1-sql dump.sql   (wrangler d1 export <db> --remote --output dump.sql)
//   node --experimental-strip-types scripts/migrate-to-local.mjs --from-sqlite old.sqlite3
// Target: ZEN_UI_DATA_DIR or %LOCALAPPDATA%\ZenBridge\zen-ui.sqlite3. Existing rows (same id) are kept (INSERT OR IGNORE).
// Audio is copied separately: Zeabur volume AUDIO_DIR → <data dir>\audio\ ; R2 → `wrangler r2 object get` per key.
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { localDatabase, localDbPath, localDataDir } from '../lib/platform/local.ts';

export const TABLES = ['people', 'sessions', 'segments', 'memories', 'memory_revisions', 'segment_revisions', 'dictionary_sources', 'dictionary_entries', 'dictionary_keys'];

/** read(table) → rows (objects keyed by column name); missing tables return []. */
export async function copyTables(read, dbPath = localDbPath(localDataDir())) {
  /** @type {Record<string, {source: number, inserted: number}>} */
  const report = {};
  const db = localDatabase(dbPath);
  for (const table of TABLES) {
    const columns = new Set(db.prepare(`PRAGMA table_info(${table})`).all().map(c => c.name));
    const rows = await read(table);
    let inserted = 0;
    db.exec('BEGIN IMMEDIATE');
    try {
      for (const row of rows) {
        const keys = Object.keys(row).filter(k => columns.has(k));
        if (!keys.length) continue;
        const values = keys.map(k => { const v = row[k]; return v === undefined ? null : typeof v === 'boolean' ? (v ? 1 : 0) : v instanceof Date ? v.toISOString() : typeof v === 'object' && v !== null && !(v instanceof Uint8Array) ? JSON.stringify(v) : v; });
        inserted += Number(db.prepare(`INSERT OR IGNORE INTO ${table}(${keys.map(k => `"${k}"`).join(',')}) VALUES(${keys.map(() => '?').join(',')})`).run(...values).changes);
      }
      db.exec('COMMIT');
    } catch (error) { db.exec('ROLLBACK'); throw error; }
    report[table] = { source: rows.length, inserted };
  }
  return report;
}
export function sqliteReader(source) {
  const has = new Set(source.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map(r => r.name));
  return async table => has.has(table) ? source.prepare(`SELECT * FROM ${table}`).all() : [];
}
export function d1DumpReader(sql) {
  const source = new DatabaseSync(':memory:');
  source.exec(sql.replace(/^\s*PRAGMA\s+defer_foreign_keys\s*=\s*\w+;?/gim, ''));
  return sqliteReader(source);
}

if (/migrate-to-local\.mjs$/.test(process.argv[1] ?? '')) {
  const [flag, value] = process.argv.slice(2);
  let read;
  if (flag === '--from-sqlite' && value) read = sqliteReader(new DatabaseSync(value, { readOnly: true }));
  else if (flag === '--from-d1-sql' && value) read = d1DumpReader(readFileSync(value, 'utf8'));
  else if (flag === '--from-postgres') {
    if (!process.env.DATABASE_URL) throw new Error('請設定 DATABASE_URL（只在這次遷移時需要）。');
    const { default: postgres } = await import('postgres');
    const sql = postgres(process.env.DATABASE_URL, { max: 1, onnotice: () => {} });
    read = async table => { try { return await sql.unsafe(`SELECT * FROM ${table}`); } catch { return []; } };
    process.once('beforeExit', () => void sql.end());
  } else {
    console.error('用法：--from-postgres | --from-d1-sql <dump.sql> | --from-sqlite <file>');
    process.exit(2);
  }
  const target = localDbPath(localDataDir());
  console.log('→', target);
  console.table(await copyTables(read, target));
}
