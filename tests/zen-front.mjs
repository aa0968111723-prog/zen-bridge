import http from 'node:http';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import WebSocket, { WebSocketServer } from 'ws';
import { createZenFront } from '../sidecar/zen-front.mjs';
import { createBreezeGateway } from '../sidecar/breeze-gateway.mjs';
const token='a'.repeat(64),gateway=createBreezeGateway({token});
const ui=http.createServer((req,res)=>res.end(req.url));
const listeners=new WebSocketServer({server:ui});
listeners.on('connection',ws=>ws.on('message',bytes=>ws.send(bytes.toString())));
ui.listen(0,'127.0.0.1');await once(ui,'listening');
const front=createZenFront(gateway,ui.address().port);
front.listen(0,'127.0.0.1');await once(front,'listening');
const base='127.0.0.1:'+front.address().port;
let agent,audience;
try {
 assert.equal(await (await fetch('http://'+base+'/api/workspace')).text(),'/api/workspace');
 const wrong=new WebSocket('ws://'+base+'/asr-agent');await new Promise(r=>wrong.once('error',r));
 assert.equal(gateway.state().connected,false);
 agent=new WebSocket('ws://'+base+'/asr-agent',{headers:{Authorization:'Bearer '+token}});await once(agent,'open');
 agent.send(JSON.stringify({type:'ready',protocol:1}));
 for(let i=0;i<20&&!gateway.state().connected;i++)await new Promise(r=>setTimeout(r,5));
 assert.equal(gateway.state().connected,true);
 audience=new WebSocket('ws://'+base+'/ws/listen');await once(audience,'open');
 const reply=once(audience,'message');audience.send('listener preserved');
 assert.equal((await reply)[0].toString(),'listener preserved');
 console.log('PASS: public UI HTTP, authenticated agent and audience WebSockets on one port');
} finally {
 agent?.terminate();audience?.terminate();
 for(const ws of listeners.clients)ws.terminate();
 await gateway.close();listeners.close();front.closeAllConnections();
 await Promise.all([new Promise(r=>front.close(r)),new Promise(r=>ui.close(r))]);
}
