import { createRequire } from 'node:module';
import { mkdir, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
const require = createRequire(import.meta.url);
const { build } = await import(pathToFileURL(createRequire(require.resolve('drizzle-kit')).resolve('esbuild')).href);
await mkdir('work', { recursive: true });
for (const suite of ['runtime', 'hermes', 'dictionary']) {
  const output = resolve('work/' + suite + '-test.mjs');
  try {
    await build({ entryPoints: ['tests/' + suite + '.test.ts'], outfile: output, bundle: true, platform: 'node', format: 'esm', packages: 'external', alias: { '@': resolve('.') }, logLevel: 'silent' });
    const result = spawnSync(process.execPath, [output], { stdio: 'inherit', env: process.env });
    process.exitCode = result.status ?? 1;
    if (process.exitCode) break;
  } finally { await rm(output, { force: true }); }
}
