import assert from 'node:assert/strict';
import { once } from 'node:events';
import { randomBytes } from 'node:crypto';
import http from 'node:http';
import WebSocket from 'ws';
import { createBreezeGateway } from '../sidecar/breeze-gateway.mjs';
const token = randomBytes(32).toString('hex');
const gateway = createBreezeGateway({ token, timeoutMs: 1000 });
gateway.server.listen(0, '127.0.0.1'); await once(gateway.server, 'listening');
const port = gateway.server.address().port, base = 'http://127.0.0.1:'+port;
const headers = { Authorization:'Bearer '+token };
async function transcribe() { const form = new FormData(); form.set('audio',new Blob([new Uint8Array(100)],{type:'audio/wav'}),'a.wav');form.set('language','en');return fetch(base+'/transcribe',{method:'POST',headers,body:form}); }
let agent;
try {
  assert.equal((await fetch(base+'/health')).status,401);
  assert.equal((await transcribe()).status,503);
  const wrong=new WebSocket('ws://127.0.0.1:'+port+'/asr-agent');
  await new Promise(resolve=>wrong.once('error',resolve));
  assert.equal(gateway.state().connected,false);
  agent=new WebSocket('ws://127.0.0.1:'+port+'/asr-agent',{headers});await once(agent,'open');
  agent.send(JSON.stringify({type:'ready',protocol:1,capabilities:['asr','translate','metrics']}));
  for(let i=0;i<20&&!gateway.state().connected;i++)await new Promise(r=>setTimeout(r,5));
  assert.equal(gateway.state().connected,true);
  const held=[];
  const receiving=[];
  // Slow uploads occupy control capacity before their bodies finish arriving.
  for(let i=0;i<2;i++){
    let respond;
    receiving.push(new Promise(resolve=>respond=resolve));
    const upload=http.request(base+'/translate',{method:'POST',headers},res=>{
      res.resume();res.on('end',()=>respond(res.statusCode));
    });
    upload.flushHeaders();upload.write('{');held.push(upload);
  }
  await new Promise(resolve=>setTimeout(resolve,30));
  assert.equal((await fetch(base+'/translate',{method:'POST',headers,body:'{}'})).status,429);
  held.forEach(upload=>upload.end('}'));
  assert.deepEqual(await Promise.all(receiving),[400,400]);
  const controlMessage=once(agent,'message');
  const translated=fetch(base+'/translate',{method:'POST',headers,body:JSON.stringify({mode:'local',instructions:'Translate only',input:'你好'})});
  const [control]=await controlMessage;const translation=JSON.parse(control);
  assert.equal(translation.type,'translate');assert.equal(translation.mode,'local');
  agent.send(JSON.stringify({type:'control_result',id:translation.id,ok:true,result:{text:'Hello',route:'local'}}));
  assert.deepEqual(await (await translated).json(),{text:'Hello',route:'local'});
  assert.equal(gateway.state().control_pending,0);
  const request=transcribe();
  const [data]=await once(agent,'message'); const job=JSON.parse(data);
  assert.equal(job.language,'en');assert.equal(job.type,'transcribe');assert.ok(job.audio);
  const queued=transcribe();
  for(let i=0;i<20&&gateway.state().waiting<1;i++)await new Promise(r=>setTimeout(r,5));
  assert.equal((await transcribe()).status,429);
  const nextMessage=once(agent,'message');
  agent.send(JSON.stringify({type:'result',id:job.id,ok:true,source:'本機辨識'}));
  assert.deepEqual(await (await request).json(),{source:'本機辨識'});
  const [next]=await nextMessage; const second=JSON.parse(next);
  agent.close();
  assert.equal((await queued).status,503);
  assert.ok(second.id!==job.id);
  console.log('PASS: authenticated agent, no cloud fallback, bounded queue, language forwarding, disconnect cleanup');
} finally { agent?.terminate();await gateway.close(); }
