import WebSocket from 'ws';
import { liveBase, liveFetch, offline } from '@/lib/breeze-proxy';

export const dynamic = 'force-dynamic';

// Breeze /ws/listen -> SSE for the turtle UI. Server-side socket (no Origin), host-side listen key.
export async function GET(request: Request) {
  const room = new URL(request.url).searchParams.get('room') || 'class';
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(room)) return Response.json({ error: 'room 格式不正確。' }, { status: 400 });
  let key = '';
  try {
    const r = await liveFetch('setup', { search: '?room_id=' + encodeURIComponent(room), signal: AbortSignal.timeout(4000) });
    if (r.ok) key = String((await r.json() as { listen_key?: string }).listen_key || '');
  } catch { return offline(); }
  const target = liveBase().replace(/^http/, 'ws') + '/ws/listen?room_id=' + encodeURIComponent(room) + '&replay=1' + (key ? '&k=' + encodeURIComponent(key) : '');
  const encoder = new TextEncoder();
  let socket: WebSocket | null = null, ping: ReturnType<typeof setInterval> | undefined;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const send = (text: string) => { try { controller.enqueue(encoder.encode('data: ' + text + '\n\n')); } catch {} };
      const end = () => { if (ping) clearInterval(ping); try { controller.close(); } catch {} };
      socket = new WebSocket(target);
      socket.on('message', data => { const text = data.toString(); if (text.length <= 200_000) send(text); });
      socket.on('close', (code) => { send(JSON.stringify({ type: 'bridge_closed', code })); end(); });
      socket.on('error', () => { send(JSON.stringify({ type: 'bridge_error' })); });
      ping = setInterval(() => { try { controller.enqueue(encoder.encode(': keep-alive\n\n')); } catch {} }, 15000);
    },
    cancel() { if (ping) clearInterval(ping); socket?.close(); },
  });
  return new Response(stream, { headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no' } });
}
