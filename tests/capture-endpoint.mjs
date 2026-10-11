// grok/fast-draft-asr: Breeze capture (vad on) ends a chunk at a pause, caps it at 4.5 s with a
// quiet-point hard cut that keeps the rest, skips leading silence, and keeps the timeline exact.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import ts from 'typescript';
const source=ts.transpileModule(fs.readFileSync(new URL('../lib/capture.ts',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
let node;
const track={label:'mic',getSettings:()=>({deviceId:'d'}),stop(){},addEventListener(){}};
const sandbox={exports:{},Blob,Float32Array,ArrayBuffer,DataView,Math,navigator:{mediaDevices:{async getUserMedia(){return {getAudioTracks:()=>[track],getTracks:()=>[track]}}}},AudioContext:class{constructor(){this.sampleRate=16000;this.destination={}}createMediaStreamSource(){return {connect(){},disconnect(){}}}createScriptProcessor(){node={connect(){},disconnect(){}};return node}createGain(){return {gain:{value:1},connect(){},disconnect(){}}}async resume(){}async close(){}}};
vm.createContext(sandbox);vm.runInContext(source,sandbox);
const BLOCK=1600; // 100 ms at 16 kHz (the real ScriptProcessor block is 4096; logic is size-agnostic)
const block=(amp)=>{const b=new Float32Array(BLOCK);for(let i=0;i<BLOCK;i++)b[i]=amp*Math.sin(i/3);return {inputBuffer:{getChannelData:()=>b}};};
const run=(cap,plan)=>{for(const [amp,blocks] of plan)for(let i=0;i<blocks;i++)node.onaudioprocess(block(amp));};

// 1. pause cut: 0.5 s silence, 1.5 s speech, 0.5 s silence -> one chunk right after the pause
{const out=[];const cap=await sandbox.exports.capture((a,offset,final)=>out.push({a,offset,final}),()=>{},{windowSeconds:6,stepSeconds:6,vad:true,endpoint:{}});
 run(cap,[[0,5],[.2,15]]);assert.equal(out.length,0);
 run(cap,[[0,3]]);assert.equal(out.length,0,'not before 0.35 s of silence');
 run(cap,[[0,1]]);assert.equal(out.length,1,'cut ~0.4 s after the last word');
 assert.ok(Math.abs(out[0].offset-0.2)<1e-9,'leading silence trimmed to a 0.3 s pre-roll; offset stays on the timeline');
 const bytes=(await out[0].a.arrayBuffer()).byteLength;assert.equal(bytes,44+(3+15+4)*BLOCK*2);
 cap.stop();}

// 2. hard cut at 4.5 s on the quietest block, remainder carried
{const out=[];const cap=await sandbox.exports.capture((a,offset)=>out.push({a,offset}),()=>{},{windowSeconds:6,stepSeconds:6,vad:true,endpoint:{}});
 run(cap,[[.2,40],[.05,1],[.2,10]]);
 assert.equal(out.length,1,'one hard cut by 4.5 s');
 const n=((await out[0].a.arrayBuffer()).byteLength-44)/2;assert.equal(n,41*BLOCK,'cut right after the quiet block (4.1 s)');
 run(cap,[[0,5]]);assert.equal(out.length,2);assert.ok(Math.abs(out[1].offset-4.1)<1e-9,'next chunk starts where the first ended');
 cap.stop();}

// 3. legacy: no vad -> fixed windows unchanged; endpoint:false opts out
{const out=[];const cap=await sandbox.exports.capture((a)=>out.push(a),()=>{},{windowSeconds:1});run(cap,[[.2,10]]);assert.equal(out.length,1);cap.stop();}
{const out=[];const cap=await sandbox.exports.capture((a)=>out.push(a),()=>{},{windowSeconds:6,vad:true,endpoint:false});run(cap,[[.2,15],[0,10]]);assert.equal(out.length,0,'opt-out keeps the 6 s window');cap.stop();}
{const out=[];const cap=await sandbox.exports.capture((a)=>out.push(a),()=>{},{windowSeconds:6,vad:true});run(cap,[[.2,15],[0,10]]);assert.equal(out.length,0,'default remains fixed windows');cap.stop();}
// 4. short tail after a soft cut closes at the next pause (does not merge into the next sentence)
{const out=[];const cap=await sandbox.exports.capture((a)=>out.push(a),()=>{},{windowSeconds:6,stepSeconds:6,vad:true,endpoint:{}});
 run(cap,[[.2,32],[0,2],[.2,5],[0,4]]);assert.equal(out.length,2,'soft cut then the 0.5 s tail');cap.stop();}
console.log('PASS: capture endpointing (pause cut, hard cut on quiet block, timeline, legacy windows)');
