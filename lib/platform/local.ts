// Local (single-machine) storage: SQLite via Node's built-in node:sqlite and audio files,
// all under the user data dir. No network, no native addon, no cloud account.
import { DatabaseSync } from 'node:sqlite';
import { mkdirSync, readdirSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import type { Command } from '../data';

export const LOCAL_DB_FILE = 'zen-ui.sqlite3';

/** ZEN_UI_DATA_DIR > %LOCALAPPDATA%\ZenBridge > $XDG_DATA_HOME/ZenBridge > ~/.local/share/ZenBridge */
export function localDataDir(env: Record<string, string | undefined> = process.env, platform: string = process.platform): string {
  if (env.ZEN_UI_DATA_DIR) return resolve(env.ZEN_UI_DATA_DIR);
  if (platform === 'win32') return join(env.LOCALAPPDATA || join(env.USERPROFILE || homedir(), 'AppData', 'Local'), 'ZenBridge');
  return join(env.XDG_DATA_HOME || join(homedir(), '.local', 'share'), 'ZenBridge');
}
export const localDbPath = (dir = localDataDir()) => join(dir, LOCAL_DB_FILE);
export const localAudioDir = (dir = localDataDir()) => join(dir, 'audio');

/** Same SQL the D1 deployment uses (drizzle/*.sql, sqlite dialect), split on drizzle breakpoints. */
export function sqliteMigrations(folder = process.env.ZEN_UI_MIGRATIONS_DIR || join(process.cwd(), 'drizzle')): { name: string; statements: string[] }[] {
  return readdirSync(folder).filter(f => /^\d{4}_.+\.sql$/.test(f)).sort().map(name => ({
    name,
    statements: readFileSync(join(folder, name), 'utf8').split(/-->\s*statement-breakpoint/).map(s => s.trim()).filter(Boolean),
  }));
}

const open = new Map<string, DatabaseSync>();
export function closeLocalDatabases() { for (const db of open.values()) db.close(); open.clear(); }

export function localDatabase(path = localDbPath(), migrationsFolder?: string): DatabaseSync {
  const key = resolve(path);
  let db = open.get(key);
  if (db) return db;
  mkdirSync(resolve(key, '..'), { recursive: true });
  db = new DatabaseSync(key);
  db.exec('PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;');
  db.exec('CREATE TABLE IF NOT EXISTS __zen_migrations (name text PRIMARY KEY, applied_at text NOT NULL)');
  const done = new Set((db.prepare('SELECT name FROM __zen_migrations').all() as { name: string }[]).map(r => r.name));
  for (const m of sqliteMigrations(migrationsFolder)) {
    if (done.has(m.name)) continue;
    db.exec('BEGIN IMMEDIATE');
    try {
      for (const sql of m.statements) db.exec(sql);
      db.prepare('INSERT INTO __zen_migrations(name,applied_at) VALUES(?,?)').run(m.name, new Date().toISOString());
      db.exec('COMMIT');
    } catch (error) { db.exec('ROLLBACK'); db.close(); throw error; }
  }
  open.set(key, db);
  return db;
}

type Value = null | number | bigint | string | Uint8Array;
const bindable = (args: unknown[]): Value[] => args.map(a => a === undefined ? null : typeof a === 'boolean' ? (a ? 1 : 0) : a as Value);
const returnsRows = (sql: string) => /^\s*(SELECT|WITH|PRAGMA|VALUES)\b/i.test(sql) || /\bRETURNING\b/i.test(sql);

export function queryLocal<T>(path: string, sql: string, args: unknown[]): T[] {
  const statement = localDatabase(path).prepare(sql);
  if (returnsRows(sql)) return statement.all(...bindable(args)) as T[];
  statement.run(...bindable(args));
  return [];
}
export function batchLocal(path: string, commands: Command[]) {
  const db = localDatabase(path);
  db.exec('BEGIN IMMEDIATE');
  try {
    for (const c of commands) db.prepare(c.sql).run(...bindable(c.args));
    db.exec('COMMIT');
  } catch (error) { db.exec('ROLLBACK'); throw error; }
}
