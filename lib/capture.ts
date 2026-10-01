export function wav(samples:Float32Array,rate:number){
 const buffer=new ArrayBuffer(44+samples.length*2),v=new DataView(buffer);
 const str=(n:number,s:string)=>{for(let i=0;i<s.length;i++)v.setUint8(n+i,s.charCodeAt(i));};
 str(0,'RIFF');v.setUint32(4,36+samples.length*2,true);str(8,'WAVE');str(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,rate,true);v.setUint32(28,rate*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);str(36,'data');v.setUint32(40,samples.length*2,true);
 for(let i=0;i<samples.length;i++)v.setInt16(44+i*2,Math.max(-1,Math.min(1,samples[i]))*32767,true);
 return new Blob([buffer],{type:'audio/wav'});
}
export type Capture={stop:()=>void;flush:()=>void};
export async function capture(onChunk:(audio:Blob,offset:number)=>void,onLevel:(level:number)=>void):Promise<Capture>{
 const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1}});
 let ctx:AudioContext|undefined;
 try{
 ctx=new AudioContext();const context=ctx,source=context.createMediaStreamSource(stream),node=context.createScriptProcessor(4096,1,1),silence=context.createGain();silence.gain.value=0;
 let chunks:Float32Array[]=[],count=0,offset=0,stopped=false;
 const flush=()=>{if(count<context.sampleRate*.3)return;const joined=new Float32Array(count);let n=0;for(const c of chunks){joined.set(c,n);n+=c.length;}const start=offset;offset+=count/context.sampleRate;chunks=[];count=0;onChunk(wav(joined,context.sampleRate),start);};
 node.onaudioprocess=e=>{if(stopped)return;const data=new Float32Array(e.inputBuffer.getChannelData(0));chunks.push(data);count+=data.length;let energy=0;for(const x of data)energy+=x*x;onLevel(Math.min(1,Math.sqrt(energy/data.length)*8));if(count>=context.sampleRate*8)flush();};
 source.connect(node);node.connect(silence);silence.connect(context.destination);await context.resume();
 return {flush,stop:()=>{if(stopped)return;stopped=true;try{flush();}finally{node.disconnect();source.disconnect();silence.disconnect();stream.getTracks().forEach(t=>t.stop());void context.close().catch(()=>{});onLevel(0);}}};
 }catch(e){stream.getTracks().forEach(t=>t.stop());if(ctx)void ctx.close().catch(()=>{});throw e;}
}
