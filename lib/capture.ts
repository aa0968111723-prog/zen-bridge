export function wav(samples:Float32Array,rate:number,targetRate=rate){
 if(targetRate!==rate){const count=Math.floor(samples.length*targetRate/rate),converted=new Float32Array(count),ratio=rate/targetRate;for(let i=0;i<count;i++){const start=Math.floor(i*ratio),end=Math.min(samples.length,Math.floor((i+1)*ratio));let total=0;for(let j=start;j<end;j++)total+=samples[j];converted[i]=total/Math.max(1,end-start);}samples=converted;rate=targetRate;}
 const buffer=new ArrayBuffer(44+samples.length*2),v=new DataView(buffer);
 const str=(n:number,s:string)=>{for(let i=0;i<s.length;i++)v.setUint8(n+i,s.charCodeAt(i));};
 str(0,'RIFF');v.setUint32(4,36+samples.length*2,true);str(8,'WAVE');str(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,rate,true);v.setUint32(28,rate*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);str(36,'data');v.setUint32(40,samples.length*2,true);
 for(let i=0;i<samples.length;i++)v.setInt16(44+i*2,Math.max(-1,Math.min(1,samples[i]))*32767,true);
 return new Blob([buffer],{type:'audio/wav'});
}
export type Capture={stop:()=>void;flush:()=>void;deviceName:string;deviceId:string};
export type CaptureOptions={deviceId?:string;monitorOnly?:boolean;onEnded?:()=>void;windowSeconds?:number;stepSeconds?:number;vad?:boolean;outputSampleRate?:number;isPaused?:()=>boolean;onPauseChange?:(paused:boolean)=>void;endpoint?:false|EndpointOptions;onPreview?:(audio:Blob,offset:number)=>void};
// grok/fast-draft-asr: with vad on (the Breeze path) a chunk ends at a pause instead of a fixed
// window, so the final caption arrives ~2-4 s after speech instead of up to 6 s. Hard cuts go to
// the quietest block in the last 0.8 s and the rest is kept for the next chunk (nothing dropped).
export type EndpointOptions={silenceMs?:number;minSeconds?:number;softMaxSeconds?:number;softSilenceMs?:number;maxSeconds?:number;loneSilenceMs?:number;loneMinSeconds?:number;preRollSeconds?:number};
export const ENDPOINT_DEFAULTS={silenceMs:350,minSeconds:.8,softMaxSeconds:3,softSilenceMs:150,maxSeconds:4.5,loneSilenceMs:1000,loneMinSeconds:.25,preRollSeconds:.3};
export type EndpointAction={emit:number;drop:number;reason:''|'pause'|'soft'|'hard'};
export function createEndpointer(rate:number,opts:EndpointOptions={}){
 const c={...ENDPOINT_DEFAULTS,...opts},rms:number[]=[],size:number[]=[];let noise=.006,start=-1,last=-1,cont=false;
 const span=(a:number,b:number)=>{let n=0;for(let i=a;i<b;i++)n+=size[i];return n/rate;};
 const shift=(k:number)=>{rms.splice(0,k);size.splice(0,k);start=start>=k?start-k:-1;last=last>=k?last-k:-1;if(start<0)last=-1;};
 const act=(emit:number,drop:number,reason:EndpointAction['reason']):EndpointAction=>{shift(emit||drop);if(emit){start=-1;last=-1;cont=reason==='soft'||reason==='hard';for(let i=0;i<rms.length;i++)if(rms[i]>=Math.max(.006,noise*3)){if(start<0)start=i;last=i;}}return {emit,drop,reason};};
 return {reset(){rms.length=0;size.length=0;start=-1;last=-1;cont=false;},push(block:Float32Array):EndpointAction{
  let e=0;for(const x of block)e+=x*x;const r=Math.sqrt(e/Math.max(1,block.length)),th=Math.max(.006,noise*3),speech=r>=th;
  if(r<noise)noise=.7*noise+.3*r;else if(!speech)noise=.995*noise+.005*r;noise=Math.max(noise,1e-4);
  rms.push(r);size.push(block.length);const i=rms.length-1;
  if(speech){if(start<0)start=i;last=i;}
  if(start<0){let keep=0,k=rms.length;while(k>0&&keep<c.preRollSeconds){k--;keep+=size[k]/rate;}return k>0?act(0,k,''):{emit:0,drop:0,reason:''};}
  const voiced=span(start,last+1),silent=span(last+1,i+1),total=span(0,i+1);
  if(voiced>=(cont?c.loneMinSeconds:c.minSeconds)&&silent*1000>=c.silenceMs)return act(i+1,0,'pause');
  if(span(start,i+1)>=c.softMaxSeconds&&silent*1000>=c.softSilenceMs)return act(i+1,0,'soft');
  if(silent*1000>=c.loneSilenceMs)return voiced>=c.loneMinSeconds?act(i+1,0,'pause'):(()=>{start=-1;last=-1;cont=false;return {emit:0,drop:0,reason:''} as EndpointAction;})();
  if(total>=c.maxSeconds){let best=i,acc=0;for(let k=i;k>start&&acc<.8;k--){acc+=size[k]/rate;if(rms[k]<rms[best])best=k;}return act(Math.max(1,best+1),0,'hard');}
  return {emit:0,drop:0,reason:''};
 }};
}
export async function capture(onChunk:(audio:Blob,offset:number,final:boolean)=>void,onLevel:(level:number)=>void,options:CaptureOptions={}):Promise<Capture>{
 const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1,...(options.deviceId&&options.deviceId!=='default'?{deviceId:{exact:options.deviceId}}:{})}});
 let ctx:AudioContext|undefined;
 try{
 ctx=new AudioContext();const context=ctx,source=context.createMediaStreamSource(stream),node=context.createScriptProcessor(4096,1,1),silence=context.createGain();silence.gain.value=0;
 let chunks:Float32Array[]=[],count=0,offset=0,stopped=false,lastSent=0,paused=false,previewSamples=0;
 const windowSeconds=options.windowSeconds??8,stepSeconds=options.stepSeconds??windowSeconds;
 let fast=false;try{fast=typeof localStorage!=='undefined'&&localStorage.getItem('zen-agent-fast-captions')==='1';}catch{/* unavailable browser storage keeps fixed windows */}
 const endpoint=options.endpoint??(fast?{}:false);
 const ep=options.vad&&endpoint!==false&&stepSeconds>=windowSeconds?createEndpointer(context.sampleRate,{...(endpoint||{}),maxSeconds:Math.min(windowSeconds,(endpoint||{}).maxSeconds??ENDPOINT_DEFAULTS.maxSeconds)}):null;
 const take=(k:number)=>{const head=chunks.slice(0,k);chunks=chunks.slice(k);let n=0;for(const c of head)n+=c.length;count-=n;return {head,n};};
 const emitBlocks=(k:number)=>{const {head,n}=take(k);if(n<context.sampleRate*.3){offset+=n/context.sampleRate;return;}const joined=new Float32Array(n);let p=0,energy=0;for(const c of head){joined.set(c,p);p+=c.length;for(const x of c)energy+=x*x;}if(Math.sqrt(energy/n)>.004)onChunk(wav(joined,context.sampleRate,options.outputSampleRate),offset,false);offset+=n/context.sampleRate;lastSent=0;};
 const emit=(retain:boolean,final=false)=>{if(count<context.sampleRate*.3)return;const joined=new Float32Array(count);let n=0,energy=0;for(const c of chunks){joined.set(c,n);n+=c.length;for(const x of c)energy+=x*x;}if(!options.vad||Math.sqrt(energy/count)>.004)onChunk(wav(joined,context.sampleRate,options.outputSampleRate),offset,final);if(retain){const step=Math.min(joined.length,Math.round(context.sampleRate*stepSeconds)),tail=joined.slice(step);chunks=tail.length?[tail]:[];count=tail.length;offset+=step/context.sampleRate;}else{offset+=count/context.sampleRate;chunks=[];count=0;}lastSent=0;};
 const flush=()=>{emit(false,true);ep?.reset();previewSamples=0;};
 const preview=()=>{if(!options.onPreview||count<context.sampleRate*.5||count>context.sampleRate*6.5||previewSamples<context.sampleRate)return;previewSamples=0;const joined=new Float32Array(count);let p=0,energy=0;for(const block of chunks){joined.set(block,p);p+=block.length;for(const sample of block)energy+=sample*sample;}if(Math.sqrt(energy/count)>.004)options.onPreview(wav(joined,context.sampleRate,16000),offset);};
 node.onaudioprocess=e=>{if(stopped)return;const data=new Float32Array(e.inputBuffer.getChannelData(0));let energy=0;for(const x of data)energy+=x*x;onLevel(Math.min(1,Math.sqrt(energy/data.length)*8));if(options.monitorOnly)return;const nextPause=options.isPaused?.()??false;if(nextPause!==paused){paused=nextPause;if(paused){offset+=count/context.sampleRate;chunks=[];count=0;lastSent=0;previewSamples=0;ep?.reset();}options.onPauseChange?.(paused);}if(paused){offset+=data.length/context.sampleRate;return;}chunks.push(data);count+=data.length;lastSent+=data.length;previewSamples+=data.length;preview();if(ep){const a=ep.push(data);if(a.drop){const {n}=take(a.drop);offset+=n/context.sampleRate;}else if(a.emit)emitBlocks(a.emit);return;}if(count>=context.sampleRate*windowSeconds&&lastSent>=context.sampleRate*stepSeconds)emit(stepSeconds<windowSeconds);};
 source.connect(node);node.connect(silence);silence.connect(context.destination);await context.resume();
 const stop=()=>{if(stopped)return;stopped=true;try{flush();}finally{node.disconnect();source.disconnect();silence.disconnect();stream.getTracks().forEach(t=>t.stop());void context.close().catch(()=>{});onLevel(0);}};
 const track=stream.getAudioTracks()[0];
 track.addEventListener('ended',()=>{stop();options.onEnded?.();},{once:true});
 return {flush,stop,deviceName:track.label||'目前的麥克風',deviceId:track.getSettings().deviceId||options.deviceId||'default'};
 }catch(e){stream.getTracks().forEach(t=>t.stop());if(ctx)void ctx.close().catch(()=>{});throw e;}
}
