import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { initializeNodeRuntime } from '../lib/env.ts';

const target = initializeNodeRuntime(process.argv.includes('--zeabur') ? 'zeabur' : undefined).DEPLOY_TARGET;
const extra = process.argv.slice(2).filter(arg => arg !== '--zeabur');
if (target === 'zeabur') {
  if (process.env.DATABASE_URL) {
    const { importDictionaries } = await import('./import-dictionaries.mjs');
    const { bootstrapDictionaries } = await import('./dictionary-bootstrap.mjs');
    const imports=new AbortController();
    for(const signal of ['SIGINT','SIGTERM'])process.once(signal,()=>imports.abort());
    void bootstrapDictionaries(importDictionaries,{signal:imports.signal}).then(r => console.log('Dictionary bootstrap:', JSON.stringify(r))).catch(() => console.error('Dictionary bootstrap failed; existing data retained'));
  }
  const port = Number(process.env.PORT || 3000);
  if (!Number.isInteger(port) || port < 1 || port > 65534) throw new Error('PORT 必須為有效的連接埠。');
  let uiPort = port;
  if (process.env.BREEZE_AGENT_TOKEN) {
    const { createBreezeGateway } = await import('../sidecar/breeze-gateway.mjs');
    const gateway = createBreezeGateway({ token: process.env.BREEZE_AGENT_TOKEN });
    await new Promise((resolve, reject) => { gateway.server.once('error', reject); gateway.server.listen(Number(process.env.BREEZE_AGENT_PORT || 8770), '0.0.0.0', resolve); });
    uiPort = port + 1;
    const { createZenFront } = await import('../sidecar/zen-front.mjs');
    const front = createZenFront(gateway, uiPort);
    await new Promise((resolve, reject) => { front.once('error', reject); front.listen(port, '0.0.0.0', resolve); });
    for (const signal of ['SIGINT','SIGTERM']) process.once(signal, () => { front.close(); void gateway.close(); });
  }
  const cli = new URL('../node_modules/vinext/dist/cli.js', import.meta.url);
  process.argv = [process.execPath, fileURLToPath(cli), 'start', '--port', String(uiPort), '--hostname', uiPort === port ? '0.0.0.0' : '127.0.0.1'];
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
