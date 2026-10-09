import http from 'node:http';
import { randomUUID, timingSafeEqual } from 'node:crypto';
import { WebSocketServer, WebSocket } from 'ws';

const MAX_AUDIO = 5_000_000;
const MAX_BODY = MAX_AUDIO + 100_000;
function equalToken(value, token) {
  const a = Buffer.from(value || ''), b = Buffer.from(token || '');
  return b.length >= 32 && a.length === b.length && timingSafeEqual(a, b);
}
function json(res, status, body) {
  if (!res.destroyed && !res.writableEnded) {
    res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify(body));
  }
}

export function createBreezeGateway({ token, timeoutMs = 120000, maxWaiting = 1 } = {}) {
  if (!token || token.length < 32 || /[\r\n]/.test(token)) throw new Error('Breeze agent token is required');
  let agent = null, ready = false, active = null, receiving = 0;
  const waiting = [];
  const sockets = new WebSocketServer({ noServer: true, maxPayload: 12_000_000 });
  const state = () => ({ connected: ready && agent?.readyState === WebSocket.OPEN, busy: !!active, waiting: waiting.length });
  const failAll = () => {
    ready = false;
    if (active) { clearTimeout(active.timer); json(active.res, 503, { error: '主持電腦已離線，請重新開啟 Zen Bridge App。' }); active = null; }
    for (const job of waiting.splice(0)) json(job.res, 503, { error: '主持電腦已離線。' });
  };
  const pump = () => {
    if (active || !state().connected) return;
    const job = waiting.shift();
    if (!job) return;
    if (job.res.destroyed) { pump(); return; }
    active = job;
    job.timer = setTimeout(() => {
      json(job.res, 504, { error: '本機辨識逾時，請檢查主持電腦。' });
      agent?.close(1011, 'Inference timeout'); failAll();
    }, timeoutMs);
    agent.send(JSON.stringify({ type: 'transcribe', id: job.id, audio: job.audio, prompt: job.prompt, language: job.language }));
  };
  const server = http.createServer(async (req, res) => {
    const local = ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(req.socket.remoteAddress);
    if (!local || !equalToken(req.headers.authorization?.replace(/^Bearer /, ''), token)) {
      req.resume(); json(res, 401, { error: '本機 gateway 認證失敗。' }); return;
    }
    const pathname = new URL(req.url, 'http://localhost').pathname;
    if (req.method === 'GET' && pathname === '/health') { json(res, 200, state()); return; }
    if (req.method !== 'POST' || pathname !== '/transcribe') { req.resume(); json(res, 404, { error: 'Unknown endpoint' }); return; }
    if (!state().connected) { req.resume(); json(res, 503, { error: '主持電腦未連線，請開啟 Zen Bridge App。' }); return; }
    if (receiving + waiting.length >= maxWaiting + (active ? 0 : 1)) { req.resume(); json(res, 429, { error: '本機辨識忙碌，請等候上一段字幕。' }); return; }
    receiving++;
    try {
      if (Number(req.headers['content-length'] || 0) > MAX_BODY) { json(res, 413, { error: 'Audio too large' }); req.resume(); return; }
      const parts = []; let bytes = 0;
      for await (const part of req) { bytes += part.length; if (bytes > MAX_BODY) throw new Error('Audio too large'); parts.push(part); }
      const form = await new Response(Buffer.concat(parts), { headers: { 'Content-Type': req.headers['content-type'] || '' } }).formData();
      const audio = form.get('audio');
      if (!(audio instanceof Blob) || !audio.size || audio.size > MAX_AUDIO) throw new Error('Invalid audio');
      const language = String(form.get('language') || 'zh');
      if (!['zh', 'en'].includes(language)) throw new Error('Invalid language');
      const job = { id: randomUUID(), res, audio: Buffer.from(await audio.arrayBuffer()).toString('base64'), prompt: String(form.get('initial_prompt') || '').slice(0, 2000), language };
      if (!state().connected) { json(res, 503, { error: '主持電腦已離線。' }); return; }
      waiting.push(job); pump();
    } catch { json(res, 400, { error: '錄音格式不正確，請使用 PCM WAV。' }); }
    finally { receiving--; }
  });
  server.on('upgrade', (req, socket, head) => {
    if (new URL(req.url, 'http://localhost').pathname !== '/asr-agent' || !equalToken(req.headers.authorization?.replace(/^Bearer /, ''), token) || agent) {
      socket.end('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n'); return;
    }
    sockets.handleUpgrade(req, socket, head, ws => {
      agent = ws; ready = false;
      const helloTimer = setTimeout(() => ws.close(1008, 'Ready handshake required'), 15000);
      ws.on('error', () => {});
      ws.on('message', bytes => {
        try {
          const message = JSON.parse(bytes.toString());
          if (message.type === 'ready' && message.protocol === 1) { clearTimeout(helloTimer); ready = true; pump(); return; }
          if (message.type !== 'result' || !active || message.id !== active.id) return;
          const job = active; active = null; clearTimeout(job.timer);
          if (message.ok === true && typeof message.source === 'string' && message.source.length <= 20000) json(job.res, 200, { source: message.source });
          else json(job.res, 503, { error: '本機辨識失敗，請檢查主持 App。' });
          pump();
        } catch { ws.close(1008, 'Invalid protocol'); }
      });
      ws.on('close', () => { clearTimeout(helloTimer); if (agent === ws) { agent = null; failAll(); } });
    });
  });
  server.requestTimeout = 30000;
  return { server, state, close: async () => {
    failAll(); for (const socket of sockets.clients) socket.terminate();
    sockets.close(); await new Promise(resolve => server.close(resolve));
  } };
}
