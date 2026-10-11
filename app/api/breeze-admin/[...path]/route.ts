import { adminBase, adminRouteAllowed, adminToken, passThrough } from '@/lib/breeze-proxy';

export const dynamic = 'force-dynamic';

async function handle(request: Request, context: { params: Promise<{ path?: string[] }> }) {
  const { path = [] } = await context.params;
  const joined = path.join('/');
  if (!adminRouteAllowed(request.method, joined)) return Response.json({ error: '不支援的後台操作。' }, { status: 404 });
  const token = adminToken();
  if (!token) return Response.json({ error: '尚未設定後台權杖 BREEZE_ADMIN_TOKEN；可先用「開啟原後台」登入。', needsToken: true }, { status: 401, headers: { 'Cache-Control': 'no-store' } });
  const url = new URL(request.url);
  try {
    const body = request.method === 'POST' ? await request.text() : undefined;
    if (body && body.length > 200_000) return Response.json({ error: '內容太大。' }, { status: 413 });
    const r = await fetch(adminBase() + '/admin/api/v1/' + joined + url.search, {
      method: request.method, body, cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(15000),
      headers: { Authorization: 'Bearer ' + token, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    });
    return passThrough(r);
  } catch {
    return Response.json({ error: '後台未啟動（127.0.0.1:8791）。', offline: true }, { status: 503, headers: { 'Cache-Control': 'no-store' } });
  }
}
export const GET = handle;
export const POST = handle;
