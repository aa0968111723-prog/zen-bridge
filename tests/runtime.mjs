import { createRequire } from 'node:module';
import { mkdir, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
const require = createRequire(import.meta.url);
const { build } = await import(createRequire(require.resolve('drizzle-kit')).resolve('esbuild'));
await mkdir('work', { recursive: true });
const output = resolve('work/runtime-test.mjs');
try {
  await build({ entryPoints: ['tests/runtime.test.ts'], outfile: output, bundle: true, platform: 'node', format: 'esm', packages: 'external', alias: { '@': resolve('.') }, logLevel: 'silent' });
  const result = spawnSync(process.execPath, [output], { stdio: 'inherit', env: process.env });
  process.exitCode = result.status ?? 1;
} finally { await rm(output, { force: true }); }
