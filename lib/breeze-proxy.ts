// Server-side bridge from the 禪譯 turtle UI to the local Breeze Live Room (8780) and its admin
// backend (8791). The browser never talks to those ports directly: the live room only accepts
// its own Origin, and the admin cookie is scoped to /admin. A server-side request without an
// Origin header is the path the live room explicitly allows for local tools.

import { getBindings } from './env';

// Worker bindings (request-scoped, Cloudflare/workerd dev) first, then the Node process env (desktop `pnpm start`).
const env = (key: string) => String((getBindings() as Record<string, unknown>)[key] || (typeof process !== 'undefined' ? process.env?.[key] : '') || '');

export function liveBase() { return (env('BREEZE_LIVE_URL') || 'http://127.0.0.1:8780').replace(/\/+$/, ''); }
export function adminBase() { return (env('BREEZE_ADMIN_URL') || 'http://127.0.0.1:8791').replace(/\/+$/, ''); }
export function adminToken() { return env('BREEZE_ADMIN_TOKEN'); }

/** Host API paths the turtle UI may reach (method + pattern). Anything else is 404. */
const LIVE_ROUTES: [string, RegExp][] = [
  ['GET', /^setup$/], ['GET', /^health$/], ['GET', /^hw\/status$/], ['GET', /^metrics$/], ['GET', /^qr$/],
  ['GET', /^export$/], ['GET', /^export-handoff\/qr$/], ['GET', /^rooms\/[A-Za-z0-9_-]{1,64}\/glossary$/],
  ['POST', /^rooms\/(open|touch|close)$/], ['POST', /^rooms\/[A-Za-z0-9_-]{1,64}\/(pause|resume)$/],
  ['POST', /^session\/(active|end)$/], ['POST', /^segment\/(missing|cancel|retranslate)$/],
  ['POST', /^export-handoff$/], ['POST', /^share-host$/], ['POST', /^push$/],
];
export function liveRouteAllowed(method: string, path: string) {
  return LIVE_ROUTES.some(([m, re]) => m === method.toUpperCase() && re.test(path));
}

/** Admin API: GET for every page; writes only on the review/glossary/TM/staging/session actions the
 *  admin UI itself offers. Token-only (Bearer); no cookie is ever forwarded. */
const ADMIN_WRITES: RegExp[] = [
  /^tm\/\d+\/(approve|reject|disable)$/, /^staging\/[A-Za-z0-9_-]+\/(approve|reject|withdraw)$/,
  /^glossary\/proposals\/\d+\/(approve|reject|merge)$/, /^corrections\/\d+\/reject$/,
  /^sessions\/[A-Za-z0-9_-]{1,64}\/(close|exports)$/, /^segments\/[A-Za-z0-9_-]+\/(undo|push|retranslate)$/,
];
export function adminRouteAllowed(method: string, path: string) {
  if (!/^[A-Za-z0-9_./-]{1,200}$/.test(path) || path.includes('..')) return false;
  const m = method.toUpperCase();
  if (m === 'GET') return !/^auth\//.test(path) && !/^users\/\d+\/tokens/.test(path) && path !== 'monitor/stream' && path !== 'live/stream';
  if (m === 'POST') return ADMIN_WRITES.some(re => re.test(path));
  return false;
}

let cachedToken = '';
export async function hostToken(force = false): Promise<string> {
  if (cachedToken && !force) return cachedToken;
  const r = await fetch(liveBase() + '/api/host-token', { cache: 'no-store', signal: AbortSignal.timeout(3000), redirect: 'manual' });
  if (!r.ok) throw new Error('host token ' + r.status); // 3xx (manual redirect) is not ok either
  const body = await r.json() as { token?: string };
  cachedToken = String(body.token || '');
  return cachedToken;
}

export function offline(message = '本機 Breeze 引擎未啟動（127.0.0.1:8780）。') {
  return Response.json({ error: message, offline: true }, { status: 503, headers: { 'Cache-Control': 'no-store' } });
}

/** Forward to the live room with the host token; one retry after a 401 (token rotated). */
export async function liveFetch(path: string, init: RequestInit & { search?: string } = {}): Promise<Response> {
  const url = liveBase() + '/api/' + path + (init.search || '');
  const go = async (token: string) => fetch(url, { ...init, cache: 'no-store', redirect: 'manual', headers: { ...(init.headers as Record<string, string> || {}), Authorization: 'Bearer ' + token } });
  let r = await go(await hostToken());
  if (r.status === 401) r = await go(await hostToken(true));
  return r;
}

export function passThrough(r: Response) {
  if (r.status >= 300 && r.status < 400) return Response.json({ error: '上游重新導向，已拒絕。' }, { status: 502 });
  const headers = new Headers({ 'Cache-Control': 'no-store' });
  const type = r.headers.get('content-type'); if (type) headers.set('Content-Type', type);
  const disposition = r.headers.get('content-disposition'); if (disposition) headers.set('Content-Disposition', disposition);
  return new Response(r.body, { status: r.status, headers });
}
