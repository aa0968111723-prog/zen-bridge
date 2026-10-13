import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import ts from 'typescript';
const fake=await import(process.env.ZEN_ARCHIVE_TEST_IDB||'fake-indexeddb');
const {indexedDB,IDBObjectStore}=fake;
function moduleAt(name){const exports={},sandbox={exports,indexedDB,Blob,crypto:webcrypto,Date,Error,Promise,ArrayBuffer,DataView,console};vm.createContext(sandbox);vm.runInContext(ts.transpileModule(fs.readFileSync(new URL('../lib/'+name+'.ts',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,sandbox);return {api:exports,sandbox};}
const {api}=moduleAt('recording-archive'),capture=moduleAt('capture').api;
let changed=0,errors=0;
const writer=await api.createRecording('session','公开錄音',()=>changed++,()=>errors++);
const a=capture.wav(new Float32Array(16000).fill(.1),16000),b=capture.wav(new Float32Array(32000).fill(.2),16000);
writer.append(a);writer.append(b);await writer.finish();await writer.finish();
assert.equal(errors,0);let rows=await api.listRecordings();assert.equal(rows.length,1);assert.equal(rows[0].chunks,2);assert.equal(rows[0].bytes,96000);assert.equal(rows[0].state,'complete');
const reopened=moduleAt('recording-archive').api;
rows=await reopened.listRecordings();const wav=await reopened.recordingWav(rows[0].id),data=new DataView(await wav.arrayBuffer());
assert.equal(wav.size,96044);assert.equal(data.getUint32(24,true),16000);assert.equal(data.getUint32(40,true),96000);assert.equal(data.getInt16(44,true),3276);assert.equal(data.getInt16(44+32000,true),6553);
// Inject a storage write fault after one committed chunk. Earlier audio must survive.
let firstCommit;const committed=new Promise(resolve=>{firstCommit=resolve;});let errorSeen;const errored=new Promise(resolve=>{errorSeen=resolve;});
const failure=await api.createRecording('session','磁碟故障',firstCommit,errorSeen);failure.append(a);await committed;
const add=IDBObjectStore.prototype.add;
IDBObjectStore.prototype.add=function(value,...rest){if(this.name==='pieces')throw Error('disk full');return add.call(this,value,...rest);};
failure.append(b);await errored;IDBObjectStore.prototype.add=add;await failure.finish();
rows=await api.listRecordings();const partial=rows.find(x=>x.label==='磁碟故障');assert.equal(partial.state,'error');assert.equal(partial.bytes,32000);assert.equal((await api.recordingWav(partial.id)).size,32044);
await api.removeRecording(partial.id);assert.equal((await api.listRecordings()).length,1);await assert.rejects(api.recordingWav(partial.id));
const unavailable=moduleAt('recording-archive');unavailable.sandbox.indexedDB=undefined;await assert.rejects(unavailable.api.createRecording('x','x',()=>{},()=>{}));
console.log('PASS: IndexedDB transaction persistence, reload/export PCM order, write failure keeps committed audio, deletion, unavailable storage');
