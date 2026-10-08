import type { AppEnv } from './env';

export function breezeHostAllowed(request: Request, env: AppEnv) {
  if (!env.BREEZE_AGENT_TOKEN) return true;
  const supplied = request.headers.get('x-zen-host') || '';
  if (supplied.length !== env.BREEZE_AGENT_TOKEN.length) return false;
  let difference = 0;
  for (let i = 0; i < supplied.length; i++) difference |= supplied.charCodeAt(i) ^ env.BREEZE_AGENT_TOKEN.charCodeAt(i);
  return difference === 0;
}

export async function breezeConnected(env: AppEnv, authorized: boolean) {
  if (!env.BREEZE_ASR_URL || !authorized) return false;
  if (!env.BREEZE_AGENT_TOKEN) return true;
  try {
    const url = new URL(env.BREEZE_ASR_URL); url.pathname = '/health';
    const response = await fetch(url, { headers: { Authorization: 'Bearer ' + env.BREEZE_AGENT_TOKEN }, signal: AbortSignal.timeout(3000), redirect: 'error' });
    return response.ok && (await response.json() as { connected?: boolean }).connected === true;
  } catch { return false; }
}
