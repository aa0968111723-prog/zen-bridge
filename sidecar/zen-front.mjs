import http from 'node:http';
import {timingSafeEqual} from 'node:crypto';

// The hosting proxy exposes one application port; route agent upgrades here.
export function createZenFront(gateway, uiPort, {hostToken=process.env.BREEZE_AGENT_TOKEN||''}={}) {
  const server = http.createServer((req, res) => {
    let path;
    try {path=decodeURIComponent(new URL(req.url,'http://localhost').pathname).replace(/\/+/g,'/').replace(/\/$/,'');}
    catch {res.writeHead(400);res.end('Invalid request path');return;}
    const publicRead=req.method==='GET'&&(path==='/api/dictionaries'||/^\/api\/room\/[a-f0-9-]{36}$/i.test(path));
    const privateRoute=path==='/mcp'||path.startsWith('/api/')&&!publicRead;
    if(hostToken&&privateRoute){
      const supplied=Buffer.from(typeof req.headers['x-zen-host']==='string'?req.headers['x-zen-host']:''),expected=Buffer.from(hostToken);
      if(supplied.length!==expected.length||!timingSafeEqual(supplied,expected)){
        res.writeHead(403,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'});
        res.end(JSON.stringify({error:'請使用已配對的 Zen Bridge 主持 App。'}));return;
      }
    }
    const upstream = http.request({ hostname: '127.0.0.1', port: uiPort,
      path: req.url, method: req.method, headers: req.headers }, reply => {
      res.writeHead(reply.statusCode, reply.headers); reply.pipe(res);
    });
    upstream.on('error', () => { if (!res.headersSent) res.writeHead(503); res.end('Application starting'); });
    req.on('aborted', () => upstream.destroy());
    res.on('close', () => upstream.destroy());
    req.pipe(upstream);
  });
  server.on('upgrade', (req, socket, head) => {
    socket.on('error', () => {});
    if (new URL(req.url, 'http://localhost').pathname === '/asr-agent') {
      gateway.server.emit('upgrade', req, socket, head); return;
    }
    const upstream = http.request({ hostname: '127.0.0.1', port: uiPort,
      path: req.url, method: req.method, headers: req.headers });
    upstream.on('upgrade', (reply, peer, extra) => {
      peer.on('error', () => socket.destroy());
      socket.write('HTTP/1.1 101 Switching Protocols\r\n' +
        Object.entries(reply.headers).map(([key, value]) => `${key}: ${value}\r\n`).join('') + '\r\n');
      if (extra.length) socket.write(extra);
      if (head.length) peer.write(head);
      socket.pipe(peer).pipe(socket);
      socket.on('close', () => peer.destroy());
    });
    upstream.on('response', reply => { socket.end(`HTTP/1.1 ${reply.statusCode} Rejected\r\nConnection: close\r\n\r\n`); reply.resume(); });
    upstream.on('error', () => socket.destroy());
    socket.on('close', () => upstream.destroy());
    upstream.end();
  });
  return server;
}
