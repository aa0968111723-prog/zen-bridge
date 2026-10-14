// Local server binding. Loopback by default; ZEN_UI_HOST=0.0.0.0 is an explicit opt-in for LAN.
export const DEFAULT_LOCAL_PORT = 8785;
const RESERVED = new Set([8645, 8780, 8791]); // 8645: never; 8780/8791: Breeze live room / admin
/** @param {Record<string, string | undefined>} [env] */
export function localPort(env = process.env) {
  const raw = env.ZEN_UI_PORT || env.PORT || String(DEFAULT_LOCAL_PORT);
  const port = Number(raw);
  if (!Number.isInteger(port) || port < 1024 || port > 65534) throw new Error('ZEN_UI_PORT 必須為 1024–65534 的連接埠。');
  if (RESERVED.has(port)) throw new Error(`連接埠 ${port} 保留給其他程式，請改用其他 ZEN_UI_PORT。`);
  return port;
}
/** @param {Record<string, string | undefined>} [env] */
export function localHost(env = process.env) {
  const host = env.ZEN_UI_HOST || '127.0.0.1';
  if (!['127.0.0.1', 'localhost', '::1', '0.0.0.0'].includes(host) && !/^(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+(\.\d+)?$/.test(host)) throw new Error('ZEN_UI_HOST 只能是本機或區域網路位址。');
  return host;
}
