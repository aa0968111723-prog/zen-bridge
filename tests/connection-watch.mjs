import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import ts from 'typescript';
const require=createRequire(import.meta.url);
function load(path,imports={},globals={}){
 const code=ts.transpileModule(fs.readFileSync(new URL(path,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 const context={exports:{},require:name=>{if(name in imports)return imports[name];throw Error('Unexpected import '+name);},...globals};
 vm.createContext(context);vm.runInContext(code,context);return context.exports;
}
let next=0;const timers=new Map();
const watcherApi=load('../lib/connection-watch.ts',{}, {AbortController,
 setTimeout:(callback,delay)=>{timers.set(++next,{callback,delay});return next;},clearTimeout:id=>timers.delete(id)});
let resolve,calls=0,visible=true;const shown=[];
const watcher=watcherApi.createConnectionWatcher(()=>{calls++;return new Promise(r=>resolve=r);},value=>shown.push(value.breeze),()=>visible);
const first=watcher.refresh();await watcher.refresh();assert.equal(calls,1,'focus must not overlap a pending request');
resolve({breeze:false});await first;assert.equal([...timers.values()][0].delay,4000);
const scheduled=[...timers.values()][0];timers.clear();scheduled.callback();
resolve({breeze:true});await new Promise(setImmediate);
assert.deepEqual(shown,[false,true],'late host readiness must update without reloading sessions');
assert.equal([...timers.values()][0].delay,15000);
visible=false;await watcher.refresh();assert.equal(calls,2,'hidden windows must not issue probes');
visible=true;const late=watcher.refresh();watcher.close();resolve({breeze:false});await late;
assert.deepEqual(shown,[false,true]);assert.equal(timers.size,0,'unmount clears scheduled work and suppresses late responses');

let healthCalls=0,statusCalls=0;
const env={BREEZE_AGENT_TOKEN:'x'.repeat(64),BREEZE_ASR_URL:'http://127.0.0.1:8081/asr',SHARE:'off'};
const host=load('../lib/breeze-host.ts',{}, {URL,AbortSignal,fetch:async()=>{healthCalls++;return Response.json({connected:true});}});
const denyDatabase=()=>{throw Error('Readiness must not query or write session data');};
const server=load('../lib/server.ts',{
 './dictionary':{},'./desktop-agent':{},zod:require('zod'),'./interpret':{},'./env':{getEnv:()=>env},
 './data':{db:denyDatabase,rows:denyDatabase,one:denyDatabase,write:denyDatabase,DatabaseUnavailableError:class extends Error{}},
 './speech/resolve':{providerName:()=> 'breeze'},'./hermes':{hermesConfigured:()=>false},
 './translation':{textTranslationProvider:()=>'',textTranslationModel:()=>''},'./rooms':{},'./breeze-host':host,
});
const route=load('../app/api/connection/route.ts',{
 '@/lib/server':{connectionStatus:async(...args)=>{statusCalls++;return server.connectionStatus(...args);},failure:()=>{throw Error('Unexpected route failure');}},
 '@/lib/env':{getEnv:()=>env},'@/lib/breeze-host':host,
},{Response});
assert.equal((await route.GET(new Request('https://example.test/api/connection'))).status,403);
assert.equal(statusCalls,0);assert.equal(healthCalls,0,'reject unpaired readers before probing');
const response=await route.GET(new Request('https://example.test/api/connection',{headers:{'x-zen-host':env.BREEZE_AGENT_TOKEN}}));
assert.equal(response.status,200);assert.equal(response.headers.get('cache-control'),'no-store');
const connection=await response.json();assert.equal(connection.breeze,true);assert.equal(connection.speech,true);
assert.equal('segments' in connection,false);assert.equal(JSON.stringify(connection).includes(env.BREEZE_AGENT_TOKEN),false);
console.log('PASS: late host readiness, bounded polling, hidden/unmounted suppression, paired-only probes without database access');
