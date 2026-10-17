import { liveFetch, liveRouteAllowed, offline, passThrough } from '@/lib/breeze-proxy';

export const dynamic = 'force-dynamic';
const MAX_BODY = 6_000_000;

async function handle(request: Request, context: { params: Promise<{ path?: string[] }> }) {
  const { path = [] } = await context.params;
  const joined = path.join('/');
  if (!liveRouteAllowed(request.method, joined)) return Response.json({ error: '不支援的 Breeze 操作。' }, { status: 404 });
  const url = new URL(request.url);
  try {
    let body: ArrayBuffer | undefined;
    const headers: Record<string, string> = {};
    if (request.method === 'POST') {
      if (Number(request.headers.get('content-length') || 0) > MAX_BODY) return Response.json({ error: '音訊太大。' }, { status: 413 });
      body = await request.arrayBuffer();
      if (body.byteLength > MAX_BODY) return Response.json({ error: '音訊太大。' }, { status: 413 });
      const type = request.headers.get('content-type'); if (type) headers['Content-Type'] = type;
      if (joined === 'push') headers['x-breeze-async-translation'] = '1';
      if (request.headers.get('x-breeze-retry') === '1') headers['x-breeze-retry'] = '1';
    }
    const r = await liveFetch(joined, { method: request.method, body, headers, search: url.search, signal: AbortSignal.timeout(joined === 'push' ? 300000 : 15000) });
    return passThrough(r);
  } catch (e) { console.error('[breeze-proxy]', e instanceof Error ? e.message : e); return offline(); }
}
export const GET = handle;
export const POST = handle;
