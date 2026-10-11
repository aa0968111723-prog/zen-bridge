'use client';
import {Fragment,useCallback,useEffect,useMemo,useRef,useState} from 'react';
import {Radio,Mic,Square,Lock,QrCode,MonitorPlay,ShieldCheck,Copy,Download,ExternalLink,Users,RefreshCw,FlaskConical,Languages,Play,CircleStop,Smartphone} from 'lucide-react';
import {toast} from 'sonner';
import {Button} from '@/components/ui/button';
import {capture,type Capture} from '@/lib/capture';
import {createCaptionStore,createRubyMemory,targetParts,canChangeTarget,demoEvents,demoFinalFor,DEMO_LABEL,JA_FONT,TARGET_LANGS,type BreezeEvent,type CaptionLine,type TargetLang,type TargetPart} from '@/lib/breeze-live';
import AsrChip from './asr-chip';
import ZenShiMark from './zen-shi-mark';
import {useBreezeSetup,postBreeze} from './use-breeze';

function TargetText({line,parts}:{line:CaptionLine;parts:TargetPart[]}){
 if(!parts.length)return <span className="breeze-pending">{line.draft?'（翻譯中…）':'翻譯尚未完成'}</span>;
 return <>{parts.map((p,i)=>p.kind==='wbr'?<wbr key={i}/>:p.kind==='ruby'?<ruby key={i}>{p.text}<rt>{p.reading}</rt></ruby>:<Fragment key={i}>{p.text}</Fragment>)}</>;
}

function newSessionId(){const d=new Date();const p=(n:number)=>String(n).padStart(2,'0');return 'zen-'+d.getFullYear()+p(d.getMonth()+1)+p(d.getDate())+'-'+p(d.getHours())+p(d.getMinutes())+p(d.getSeconds());}

export default function LiveRoomPanel({onOpenAdmin,demoDefault=false}:{onOpenAdmin?:()=>void;demoDefault?:boolean}){
 const [room,setRoom]=useState('class'),[lang,setLang]=useState<TargetLang>('en'),[sessionId,setSessionId]=useState(''),[active,setActive]=useState(false);
 const [demo,setDemo]=useState(demoDefault),[lines,setLines]=useState<CaptionLine[]>([]),[stream,setStream]=useState<'off'|'connecting'|'live'|'closed'>('off');
 const [recording,setRecording]=useState(false),[level,setLevel]=useState(0),[pending,setPending]=useState(0),[handoff,setHandoff]=useState(''),[qrVariant,setQrVariant]=useState<'ip'|'name'>('ip'),[qrNonce,setQrNonce]=useState(0);
 const {setup,offline,checked,refresh}=useBreezeSetup(room,!demo);
 const store=useRef(createCaptionStore()),seq=useRef(0),recorder=useRef<Capture|null>(null),started=useRef(0),mounted=useRef(true);
 // Ruby only on the first line of the session where a term appears: walk lines oldest-first.
 const partsById=useMemo(()=>{const m=createRubyMemory(),out=new Map<string,TargetPart[]>();for(const l of lines)out.set(l.id,targetParts(l,m));return out;},[lines]);
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;recorder.current?.stop();};},[]);
 // Server language wins while a session runs (one target language per session).
 useEffect(()=>{if(!demo&&setup?.tgt_lang&&setup.tgt_lang!==lang&&!active)queueMicrotask(()=>setLang(setup.tgt_lang as TargetLang));},[setup?.tgt_lang,demo,active,lang]);
 // Demo: labelled fake ASR/MT; the grey draft turns into its final after 2.5 s (in place).
 useEffect(()=>{if(!demo)return;const s=createCaptionStore();demoEvents(lang).forEach(e=>s.apply(e));store.current=s;queueMicrotask(()=>{setLines(s.lines());setStream('live');});const t=setTimeout(()=>{s.apply(demoFinalFor(lang));setLines(s.lines());},2500);return()=>clearTimeout(t);},[demo,lang]);
 // Live: SSE bridge of Breeze /ws/listen.
 useEffect(()=>{
  if(demo)return;const s=createCaptionStore();store.current=s;queueMicrotask(()=>{setLines([]);setStream('connecting');});
  let source:EventSource|null=null,retry:ReturnType<typeof setTimeout>|undefined,closed=false;
  const connect=()=>{source=new EventSource('/api/breeze/listen?room='+encodeURIComponent(room));
   source.onmessage=ev=>{let data:BreezeEvent;try{data=JSON.parse(ev.data);}catch{return;}
    if(data.type==='hello'){setStream('live');for(const h of data.history||[])s.apply(h);setLines(s.lines());return;}
    if(data.type==='bridge_closed'||data.type==='room_unavailable'){setStream('closed');return;}
    if(s.apply(data))setLines(s.lines());};
   source.onerror=()=>{source?.close();if(!closed){setStream('closed');retry=setTimeout(connect,4000);}};};
  connect();return()=>{closed=true;source?.close();if(retry)clearTimeout(retry);};
 },[room,demo]);
 const shown=lines.slice(-8),current=shown.at(-1);
 const ja=lang==='ja',jaStyle=ja?{fontFamily:JA_FONT}:undefined;
 const listenUrl=setup?.listen_url||'',liveOrigin=listenUrl?new URL(listenUrl).origin:'http://127.0.0.1:'+(setup?.port||8780);
 const overlayUrl=liveOrigin+'/overlay/'+encodeURIComponent(room)+(setup?.listen_key?'?k='+encodeURIComponent(setup.listen_key):'');
 const connected=demo||(!!setup&&!offline);

 function chooseLang(next:string){const verdict=canChangeTarget(active,lang,next);if(!verdict.ok){toast.info(verdict.reason);return;}setLang(next as TargetLang);}
 async function startSession(){
  if(demo){setActive(true);setSessionId('demo-session');toast.success('示範場次開始（'+(ja?'日文':'英文')+'，已鎖定）');return;}
  try{const sid=newSessionId();await postBreeze('rooms/open',{room_id:room,tgt_lang:lang});await postBreeze('session/active',{room_id:room,active:true});setSessionId(sid);seq.current=0;setActive(true);void refresh();toast.success('本場開始：目標語言 '+(ja?'日文':'英文')+'，本場不可更換。');}
  catch(e){toast.error(e instanceof Error?e.message:'無法開始本場');}
 }
 async function endSession(){
  stopMic();
  if(demo){setActive(false);toast.success('示範場次結束');return;}
  try{if(sessionId)await postBreeze('session/end',{room_id:room,session_id:sessionId,last_seq:seq.current||null});else await postBreeze('session/active',{room_id:room,active:false});toast.success('本場已結束並保存。');}
  catch(e){toast.error(e instanceof Error?e.message:'結束失敗');}finally{setActive(false);void refresh();}
 }
 const stopMic=useCallback(()=>{const r=recorder.current;recorder.current=null;r?.stop();setRecording(false);setLevel(0);},[]);
 async function startMic(){
  if(demo){toast.info('示範模式不收音；請關閉示範並啟動 Breeze 引擎。');return;}
  if(!active){toast.info('請先「開始本場」，鎖定目標語言。');return;}
  try{started.current=Date.now();recorder.current=await capture((blob,offset)=>{
   const n=++seq.current,f=new FormData();f.set('room_id',room);f.set('session_id',sessionId);f.set('seq',String(n));f.set('t0_ms',String(Math.round(offset*1000)));f.set('t1_ms',String(Date.now()-started.current));f.set('wait_translation','0');f.set('audio',blob,'segment.wav');
   setPending(p=>p+1);
   void fetch('/api/breeze/push',{method:'POST',body:f}).then(async r=>{if(!r.ok){const b=await r.json().catch(()=>({})) as {detail?:string;error?:string};toast.error(b.detail||b.error||'送出失敗（'+r.status+'）');}}).catch(()=>toast.error('Breeze 引擎離線')).finally(()=>{if(mounted.current)setPending(p=>Math.max(0,p-1));});
  },setLevel,{windowSeconds:6,stepSeconds:6,vad:true,outputSampleRate:16000,onEnded:()=>{stopMic();toast.error('麥克風已中斷');}});setRecording(true);}
  catch{toast.error('請允許麥克風權限。');}
 }
 async function makeHandoff(){
  if(demo){toast.info('示範模式：交接 QR 需連線 Breeze 引擎。');return;}
  try{const r=await postBreeze<{token:string;url:string|null}>('export-handoff',{room_id:room,kind:'srt'});const img=await fetch('/api/breeze/export-handoff/qr?token_id='+encodeURIComponent(r.token),{cache:'no-store'});if(!img.ok)throw new Error('尚無可供手機使用的連結');setHandoff(URL.createObjectURL(await img.blob()));toast.success('下載交接 QR：10 分鐘內、只能用一次。');}
  catch(e){toast.error(e instanceof Error?e.message:'無法產生交接 QR');}
 }
 async function copy(text:string){try{await navigator.clipboard.writeText(text);toast.success('已複製。');}catch{toast.info('請直接選取文字複製。');}}

 return <div className="breeze-live">
  <section className="collection-paper breeze-hero">
   <div className="breeze-hero-head">
    <div className="breeze-brand"><ZenShiMark size={48}/><div><span className="handwritten">禪釋 × 龜龜・同一個 App</span><h2>Breeze 現場字幕</h2><p>本機 Breeze 引擎辨識與翻譯；灰色草稿會在原位被定稿取代。</p></div></div>
    <div className="breeze-status-row">
     <span className={'connection-state '+(connected?'ready':'')}><Radio size={13}/> {demo?'示範模式':connected?'Breeze 已連線':checked?'Breeze 未啟動':'檢查中…'}</span>
     {demo?<AsrChip gpu={{requested:'vulkan',active:'vulkan',ok:true,device:'AMD Radeon (Vega 7)',selftest_ms:412}} compact/>:<AsrChip gpu={setup?.asr_gpu} mode={setup?.asr_mode} compact/>}
     <span className="connection-state"><Users size={13}/> 聽眾 {demo?12:setup?.listeners??0}</span>
     <label className="breeze-demo-toggle"><input type="checkbox" checked={demo} onChange={e=>{setDemo(e.target.checked);setActive(false);}}/> <FlaskConical size={14}/>示範資料</label>
    </div>
   </div>
   {demo&&<p className="breeze-demo-note" role="note">{DEMO_LABEL}：以下字幕、聽眾數與 GPU 狀態皆為示範，用來預覽版面。</p>}
   {!demo&&offline&&checked&&<p className="breeze-offline" role="status">找不到本機 Breeze 引擎（127.0.0.1:8780）。請開啟 Breeze Live Room，或勾選「示範資料」預覽。<button className="text-link" onClick={()=>void refresh()}><RefreshCw size={13}/>重試</button></p>}
   <div className="breeze-controls">
    <label className="field-label">房間<input className="breeze-input" value={room} disabled={active} maxLength={64} onChange={e=>setRoom(e.target.value.replace(/[^A-Za-z0-9_-]/g,'')||'class')}/></label>
    <div className="field-label">目標語言（每場一種）<div className="breeze-lang" role="radiogroup" aria-label="目標語言">{TARGET_LANGS.map(t=><button key={t.value} role="radio" aria-checked={lang===t.value} className={lang===t.value?'selected':''} disabled={active&&lang!==t.value} onClick={()=>chooseLang(t.value)}>{t.label}</button>)}{active&&<span className="breeze-lock"><Lock size={13}/>本場已鎖定</span>}</div></div>
    <div className="breeze-session-buttons">
     {!active?<Button onClick={()=>void startSession()} disabled={!connected}><Play size={16}/>開始本場</Button>:<Button variant="outline" onClick={()=>void endSession()}><CircleStop size={16}/>結束本場</Button>}
     <Button className={'record-button '+(recording?'record-stop':'')} disabled={!active||demo} onClick={()=>recording?stopMic():void startMic()}>{recording?<><Square size={16}/>停止收音</>:<><Mic size={16}/>Breeze 收音</>}</Button>
     <span className="breeze-meter" aria-label={'音量 '+Math.round(level*100)+'%'}>{Array.from({length:7},(_,i)=><i key={i} style={{height:recording?6+Math.min(22,level*30)*(0.4+(i%3)*.3):6}}/>)}</span>
     {pending>0&&<small className="breeze-pending">送出中 {pending}</small>}
    </div>
   </div>
  </section>

  <div className="breeze-grid">
   <section className="translation-paper breeze-stage" aria-label="Breeze 即時字幕">
    <div className="paper-top"><div className="paper-label"><span className="turtle-mood mood-listen paper-mascot" aria-hidden/><span className="stamp">Breeze 即時字幕</span><span className="capture-state" role="status">{stream==='live'?'串流中':stream==='connecting'?'連線中':stream==='closed'?'等待房間開啟':'未連線'}</span></div><span className="paper-direction"><Languages size={15}/> 中 → {ja?'日本語':'EN'}</span></div>
    {current?<div className="breeze-current" aria-live="polite">
     <p className={'breeze-zh'+(current.draft?' is-draft':'')} lang="zh-Hant">{current.zh||'…'}</p>
     <p className={'breeze-target'+(ja?' is-ja':'')} lang={ja?'ja':'en'} style={jaStyle}><TargetText line={current} parts={partsById.get(current.id)||[]}/></p>
    </div>:<div className="breeze-empty"><img src="/art/poster-girl-180.webp" alt="" width={90} height={102}/><span className="turtle-mood mood-wave" aria-hidden/><p>{active?'請開始收音，第一句字幕會出現在這裡。':'開始本場後，Breeze 的字幕會在這裡同步顯示。'}</p></div>}
    <ol className="breeze-lines">{shown.slice(0,-1).reverse().map(l=><li key={l.id} className={l.draft?'is-draft':''}><span className="breeze-seq">#{l.seq}</span><p lang="zh-Hant">{l.zh}</p><p lang={l.tgt_lang} style={l.tgt_lang==='ja'?{fontFamily:JA_FONT}:undefined} className={l.tgt_lang==='ja'?'is-ja':''}><TargetText line={l} parts={partsById.get(l.id)||[]}/></p></li>)}</ol>
    <div className="paper-bottom"><span>{demo?DEMO_LABEL:'灰字＝草稿（尚未定稿）；定稿會在原位取代。'}</span><div className="paper-actions">{(['srt','vtt','txt'] as const).map(k=><a key={k} className="breeze-dl" href={demo?undefined:'/api/breeze/export?room_id='+encodeURIComponent(room)+'&kind='+k} aria-disabled={demo}><Download size={14}/>{k.toUpperCase()}</a>)}</div></div>
   </section>

   <aside className="breeze-side">
    <section className="speaker-note breeze-card"><div className="note-title"><span className="note-number">QR</span><h2>聽眾房</h2></div>
     <div className="breeze-qr-switch"><button className={qrVariant==='ip'?'selected':''} onClick={()=>setQrVariant('ip')}>IP 位址</button><button className={qrVariant==='name'?'selected':''} onClick={()=>setQrVariant('name')}>電腦名稱</button><button aria-label="重新產生 QR" onClick={()=>setQrNonce(n=>n+1)}><RefreshCw size={13}/></button></div>
     {demo?<div className="breeze-qr demo-qr" aria-label="示範 QR"><QrCode size={120}/><small>示範</small></div>:setup?.share_ready?<img className="breeze-qr" src={'/api/breeze/qr?room_id='+encodeURIComponent(room)+'&variant='+qrVariant+'&n='+qrNonce} width={170} height={170} alt="聽眾房 QR Code"/>:<p className="fine-note">{setup?.share_message||'尚無可供其他裝置使用的連結'}</p>}
     {listenUrl&&<button className="text-link" onClick={()=>void copy(qrVariant==='name'&&setup?.listen_url_name?setup.listen_url_name:listenUrl)}><Copy size={13}/>複製聽眾連結</button>}
    </section>
    <section className="lesson-note breeze-card"><div className="note-title"><span className="note-number"><Smartphone size={14}/></span><h2>下載交接 QR</h2></div>
     <p>讓手機掃描後下載本場 SRT；10 分鐘有效、只能用一次。</p>
     {handoff?<img className="breeze-qr" src={handoff} width={150} height={150} alt="下載交接 QR"/>:null}
     <Button variant="outline" onClick={()=>void makeHandoff()}><QrCode size={15}/>產生交接 QR</Button>
    </section>
    <section className="lesson-note breeze-card"><div className="note-title"><span className="note-number"><MonitorPlay size={14}/></span><h2>OBS 字幕疊加</h2></div>
     <p>在 OBS 新增「瀏覽器來源」，貼上下列網址（透明背景）。</p>
     <code className="breeze-url">{demo?'http://127.0.0.1:8780/overlay/class?k=…':overlayUrl}</code>
     <div className="button-group"><Button variant="outline" onClick={()=>void copy(overlayUrl)}><Copy size={15}/>複製</Button><Button variant="ghost" onClick={()=>window.open(overlayUrl,'_blank','noopener')}><ExternalLink size={15}/>預覽</Button></div>
    </section>
    <section className="companion-note breeze-card"><ShieldCheck size={22}/><div><span className="handwritten">管理後台</span><p>場次、審核、詞彙表、翻譯記憶、匯出、效能都在「管理後台」分頁。</p><div className="button-group"><Button variant="outline" onClick={onOpenAdmin}>前往管理後台</Button><Button variant="ghost" onClick={()=>window.open('http://127.0.0.1:8791/admin','_blank','noopener')}><ExternalLink size={15}/>原後台</Button></div></div></section>
   </aside>
  </div>
 </div>;
}
