import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { getEnv } from '../lib/env.ts';

const target = getEnv().DEPLOY_TARGET;
if (process.argv.includes('--zeabur') && target !== 'zeabur') {
  throw new Error('DEPLOY_TARGET：start:zeabur 需要 zeabur，或設定 DATABASE_URL。');
}
const extra = process.argv.slice(2).filter(arg => arg !== '--zeabur');
if (target === 'zeabur') {
  const port = Number(process.env.PORT || 3000);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('PORT 必須為有效的連接埠。');
  const cli = new URL('../node_modules/vinext/dist/cli.js', import.meta.url);
  process.argv = [process.execPath, fileURLToPath(cli), 'start', '--port', String(port), '--hostname', '0.0.0.0'];
  await import(cli.href);
} else {
  const hasFlag = name => extra.some(arg => arg === '--' + name || arg.startsWith('--' + name + '='));
  const args = ['--import', fileURLToPath(new URL('./sites-env.mjs', import.meta.url)),
    fileURLToPath(new URL('../node_modules/wrangler/bin/wrangler.js', import.meta.url)),
    'dev', '--config', 'dist/server/wrangler.json', '--local', '--persist-to', '.wrangler/state', '--inspector-port', '0'];
  if (!hasFlag('ip')) args.push('--ip', '127.0.0.1');
  if (!hasFlag('port')) args.push('--port', process.env.PORT || process.env.WEB_PORT || '8787');
  args.push(...extra);
  const child = spawn(process.execPath, args, { stdio: 'inherit' });
  child.on('exit', (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    else process.exit(code ?? 1);
  });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
}
