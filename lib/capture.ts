export function wav(samples:Float32Array,rate:number,targetRate=rate){
 if(targetRate!==rate){const count=Math.floor(samples.length*targetRate/rate),converted=new Float32Array(count),ratio=rate/targetRate;for(let i=0;i<count;i++){const start=Math.floor(i*ratio),end=Math.min(samples.length,Math.floor((i+1)*ratio));let total=0;for(let j=start;j<end;j++)total+=samples[j];converted[i]=total/Math.max(1,end-start);}samples=converted;rate=targetRate;}
 const buffer=new ArrayBuffer(44+samples.length*2),v=new DataView(buffer);
 const str=(n:number,s:string)=>{for(let i=0;i<s.length;i++)v.setUint8(n+i,s.charCodeAt(i));};
 str(0,'RIFF');v.setUint32(4,36+samples.length*2,true);str(8,'WAVE');str(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,rate,true);v.setUint32(28,rate*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);str(36,'data');v.setUint32(40,samples.length*2,true);
 for(let i=0;i<samples.length;i++)v.setInt16(44+i*2,Math.max(-1,Math.min(1,samples[i]))*32767,true);
 return new Blob([buffer],{type:'audio/wav'});
}
export type Capture={stop:()=>void;flush:()=>void;deviceName:string;deviceId:string};
export type CaptureOptions={deviceId?:string;monitorOnly?:boolean;onEnded?:()=>void;windowSeconds?:number;stepSeconds?:number;vad?:boolean;outputSampleRate?:number;isPaused?:()=>boolean;onPauseChange?:(paused:boolean)=>void};
export async function capture(onChunk:(audio:Blob,offset:number,final:boolean)=>void,onLevel:(level:number)=>void,options:CaptureOptions={}):Promise<Capture>{
 const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1,...(options.deviceId&&options.deviceId!=='default'?{deviceId:{exact:options.deviceId}}:{})}});
 let ctx:AudioContext|undefined;
 try{
 ctx=new AudioContext();const context=ctx,source=context.createMediaStreamSource(stream),node=context.createScriptProcessor(4096,1,1),silence=context.createGain();silence.gain.value=0;
 let chunks:Float32Array[]=[],count=0,offset=0,stopped=false,lastSent=0,paused=false;
 const windowSeconds=options.windowSeconds??8,stepSeconds=options.stepSeconds??windowSeconds;
 const emit=(retain:boolean,final=false)=>{if(count<context.sampleRate*.3)return;const joined=new Float32Array(count);let n=0,energy=0;for(const c of chunks){joined.set(c,n);n+=c.length;for(const x of c)energy+=x*x;}if(!options.vad||Math.sqrt(energy/count)>.004)onChunk(wav(joined,context.sampleRate,options.outputSampleRate),offset,final);if(retain){const step=Math.min(joined.length,Math.round(context.sampleRate*stepSeconds)),tail=joined.slice(step);chunks=tail.length?[tail]:[];count=tail.length;offset+=step/context.sampleRate;}else{offset+=count/context.sampleRate;chunks=[];count=0;}lastSent=0;};
 const flush=()=>emit(false,true);
 node.onaudioprocess=e=>{if(stopped)return;const data=new Float32Array(e.inputBuffer.getChannelData(0));let energy=0;for(const x of data)energy+=x*x;onLevel(Math.min(1,Math.sqrt(energy/data.length)*8));if(options.monitorOnly)return;const nextPause=options.isPaused?.()??false;if(nextPause!==paused){paused=nextPause;options.onPauseChange?.(paused);}if(paused){offset+=data.length/context.sampleRate;return;}chunks.push(data);count+=data.length;lastSent+=data.length;if(count>=context.sampleRate*windowSeconds&&lastSent>=context.sampleRate*stepSeconds)emit(stepSeconds<windowSeconds);};
 source.connect(node);node.connect(silence);silence.connect(context.destination);await context.resume();
 const stop=()=>{if(stopped)return;stopped=true;try{flush();}finally{node.disconnect();source.disconnect();silence.disconnect();stream.getTracks().forEach(t=>t.stop());void context.close().catch(()=>{});onLevel(0);}};
 const track=stream.getAudioTracks()[0];
 track.addEventListener('ended',()=>{stop();options.onEnded?.();},{once:true});
 return {flush,stop,deviceName:track.label||'目前的麥克風',deviceId:track.getSettings().deviceId||options.deviceId||'default'};
 }catch(e){stream.getTracks().forEach(t=>t.stop());if(ctx)void ctx.close().catch(()=>{});throw e;}
}
