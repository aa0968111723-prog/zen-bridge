import { QWEN_MODEL, type DeployTarget } from './env';

export type QwenLiveResult = {source: string; translation: string};
export type LiveSocket = {
  send(data: string): void;
  close(): void;
  addEventListener(type: string, listener: (event: { data?: unknown; code?: number }) => void): void;
};
type Options={apiKey:string;region?:string;model?:string;platform:DeployTarget;direction:'zh-en'|'en-zh';phrases:Record<string,string>};

export function qwenEndpoint(region?: string, model = QWEN_MODEL) {
  const host = region === 'cn' ? 'dashscope.aliyuncs.com' : 'dashscope-intl.aliyuncs.com';
  return `https://${host}/api-ws/v1/realtime?model=${encodeURIComponent(model)}`;
}

export function wavToPcm16k(bytes: Uint8Array) {
  if(bytes.length<44||String.fromCharCode(...bytes.slice(0,4))!=='RIFF'||String.fromCharCode(...bytes.slice(8,12))!=='WAVE')throw new Error('請送出 16-bit PCM WAV，不能是壓縮音檔。');
  const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
  let offset=12,sampleRate=0,channels=0,bits=0,format=0,dataStart=0,dataSize=0;
  while(offset+8<=bytes.length){
    const id=String.fromCharCode(bytes[offset],bytes[offset+1],bytes[offset+2],bytes[offset+3]),size=view.getUint32(offset+4,true);
    if(offset+8+size>bytes.length)throw new Error('WAV 音檔不完整。');
    if(id==='fmt '){if(size<16)throw new Error('WAV 音檔格式不正確。');format=view.getUint16(offset+8,true);channels=view.getUint16(offset+10,true);sampleRate=view.getUint32(offset+12,true);bits=view.getUint16(offset+22,true);}
    else if(id==='data'){dataStart=offset+8;dataSize=size;break;}
    offset+=8+size+(size%2);
  }
  if(!dataStart||!dataSize||format!==1||bits!==16||channels<1||sampleRate<1||dataSize%(2*channels))throw new Error('語音片段需為 16-bit PCM WAV。');
  const samples=Math.floor(dataSize/2/channels),mono=new Int16Array(samples);
  for(let i=0;i<samples;i++){let sum=0;for(let c=0;c<channels;c++)sum+=view.getInt16(dataStart+(i*channels+c)*2,true);mono[i]=Math.max(-32768,Math.min(32767,Math.round(sum/channels)));}
  if(sampleRate===16000)return mono;
  const output=new Int16Array(Math.max(1,Math.round(mono.length*16000/sampleRate)));
  for(let i=0;i<output.length;i++){const pos=i*sampleRate/16000,left=Math.min(mono.length-1,Math.floor(pos)),right=Math.min(mono.length-1,left+1),mix=pos-left;output[i]=Math.round(mono[left]*(1-mix)+mono[right]*mix);}
  return output;
}

export function pcmBase64(samples:Int16Array,start:number,end:number){
  const slice=samples.subarray(start,end),bytes=new Uint8Array(slice.buffer,slice.byteOffset,slice.byteLength);
  let binary='';for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode(...bytes.subarray(i,i+0x8000));return btoa(binary);
}

async function connect(options:Options){
  const endpoint=qwenEndpoint(options.region,options.model);
  if(options.platform==='zeabur'){const {openNodeSocket}=await import('@/lib/platform/node');return openNodeSocket(endpoint,options.apiKey);}
  const response=await fetch(endpoint,{headers:{Upgrade:'websocket',Authorization:'Bearer '+options.apiKey},signal:AbortSignal.timeout(15000)});
  if(!response.webSocket)throw new Error('Cloudflare 無法連線至阿里雲。');
  response.webSocket.accept();return response.webSocket as LiveSocket;
}

export async function qwenLiveTranslate(file:Uint8Array,options:Options){
  const pcm=wavToPcm16k(file),platform=options.platform==='zeabur'?'Zeabur':'Cloudflare',connectionError=()=>new Error(`${platform} 無法連線至阿里雲。請檢查平台網路與 DASHSCOPE_API_KEY。`);
  let socket:LiveSocket;try{socket=await connect(options);}catch{throw connectionError();}
  return new Promise<QwenLiveResult>((resolve,reject)=>{
    let source='',translation='',settled=false;
    const finish=(error?:Error)=>{if(settled)return;settled=true;clearTimeout(timer);try{socket.close();}catch{}if(error)reject(error);else resolve({source:source.trim(),translation:translation.trim()});};
    const timer=setTimeout(()=>finish(new Error('阿里雲同傳超時，請稍後再試。')),50000);
    socket.addEventListener('message',event=>{if(typeof event.data!=='string')return;let body:{type?:string;transcript?:string;text?:string;delta?:string};try{body=JSON.parse(event.data);}catch{return;}
      if(body.type==='error'){finish(new Error('阿里雲同傳回傳錯誤。請檢查 DASHSCOPE_API_KEY、QWEN_LIVE_MODEL 與 DASHSCOPE_REGION。'));return;}
      if(body.type==='conversation.item.input_audio_transcription.completed')source=body.transcript||source;
      if(body.type==='response.audio_transcript.done'||body.type==='response.text.done')translation=body.transcript||body.text||translation;
      if(body.type==='response.audio_transcript.delta'||body.type==='response.text.delta')translation+=body.delta||'';
      if(body.type==='session.finished')finish();
    });
    socket.addEventListener('error',()=>finish(connectionError()));socket.addEventListener('close',event=>{if(!settled)finish(event.code===1000&&(source||translation)?undefined:connectionError());});
    try{
      socket.send(JSON.stringify({type:'session.update',session:{output_modalities:['text'],translation:{language:options.direction==='zh-en'?'en':'zh',corpus:{phrases:options.phrases}}}}));
      for(let i=0;i<pcm.length;i+=1600)socket.send(JSON.stringify({type:'input_audio_buffer.append',audio:pcmBase64(pcm,i,Math.min(pcm.length,i+1600))}));
      socket.send(JSON.stringify({type:'session.finish'}));
    }catch{finish(connectionError());}
  });
}

type Pending={source:string;translation:string;resolve:(value:QwenLiveResult)=>void;reject:(error:Error)=>void;timer:ReturnType<typeof setTimeout>};
type StreamSession={socket:LiveSocket;created:number;pending:Pending|null;queue:Promise<QwenLiveResult>};
const streams=new Map<string,StreamSession>();

async function openStream(options:Options,key:string){
  const socket=await connect(options),session:StreamSession={socket,created:Date.now(),pending:null,queue:Promise.resolve({source:'',translation:''})};
  const fail=()=>{const error=new Error('Qwen Live 串流已中斷；請重送最後一個已確認句子。');if(session.pending){clearTimeout(session.pending.timer);session.pending.reject(error);session.pending=null;}streams.delete(key);};
  socket.addEventListener('message',event=>{if(!session.pending||typeof event.data!=='string')return;let body:{type?:string;transcript?:string;text?:string;delta?:string};try{body=JSON.parse(event.data);}catch{return;}
    if(body.type==='error'){fail();try{socket.close();}catch{}return;}
    if(body.type==='conversation.item.input_audio_transcription.completed')session.pending.source=body.transcript||session.pending.source;
    if(body.type==='response.text.delta')session.pending.translation+=body.delta||'';
    if(body.type==='response.text.done'){session.pending.translation=body.text||session.pending.translation;const pending=session.pending;session.pending=null;clearTimeout(pending.timer);pending.resolve({source:pending.source.trim(),translation:pending.translation.trim()});}
  });
  socket.addEventListener('error',fail);socket.addEventListener('close',fail);
  socket.send(JSON.stringify({type:'session.update',session:{output_modalities:['text'],translation:{language:options.direction==='zh-en'?'en':'zh',corpus:{phrases:options.phrases}}}}));
  streams.set(key,session);return session;
}

export async function qwenStreamTranslate(key:string,file:Uint8Array,options:Options){
  let session=streams.get(key);
  if(session&&Date.now()-session.created>=2*60*60*1000){session.socket.close();streams.delete(key);session=undefined;}
  session??=await openStream(options,key);
  const active=session,previous=active.queue.catch(()=>({source:'',translation:''}));
  active.queue=previous.then(()=>new Promise<QwenLiveResult>((resolve,reject)=>{
    const timer=setTimeout(()=>{if(active.pending){active.pending=null;streams.delete(key);try{active.socket.close();}catch{}reject(new Error('Qwen Live 串流等待句尾超時。'));}},60000);
    active.pending={source:'',translation:'',resolve,reject,timer};
    try{const pcm=wavToPcm16k(file);for(let i=0;i<pcm.length;i+=1600)active.socket.send(JSON.stringify({type:'input_audio_buffer.append',audio:pcmBase64(pcm,i,Math.min(pcm.length,i+1600))}));active.socket.send(JSON.stringify({type:'input_audio_buffer.commit'}));active.socket.send(JSON.stringify({type:'response.create',response:{output_modalities:['text']}}));}
    catch{clearTimeout(timer);active.pending=null;reject(new Error('Qwen Live 串流送出失敗。'));}
  }));
  return active.queue;
}
