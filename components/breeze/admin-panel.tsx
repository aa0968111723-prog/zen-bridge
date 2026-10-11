'use client';
import {useCallback,useEffect,useState} from 'react';
import {LayoutDashboard,Gauge,ListVideo,ClipboardCheck,Inbox,BookA,Database,Search,FileDown,Settings2,ScrollText,UserCog,ExternalLink,RefreshCw,FlaskConical,Check,X,KeyRound} from 'lucide-react';
import {toast} from 'sonner';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import AsrChip from './asr-chip';
import ZenShiMark from './zen-shi-mark';
import {ADMIN_DEMO} from '@/lib/breeze-admin-demo';
import type {GpuStatus} from '@/lib/breeze-live';

type Page={id:string;label:string;icon:typeof Gauge;endpoint:string;hint:string;native:string};
export const ADMIN_PAGES:Page[]=[
 {id:'overview',label:'總覽',icon:LayoutDashboard,endpoint:'overview',hint:'元件狀態、即時 RTF、ASR 加速',native:'/admin#/overview'},
 {id:'perf',label:'效能',icon:Gauge,endpoint:'perf/metrics',hint:'ASR／MT 延遲與資源',native:'/admin/perf'},
 {id:'sessions',label:'場次',icon:ListVideo,endpoint:'sessions',hint:'每一場社課與段落數',native:'/admin#/sessions'},
 {id:'review',label:'審核',icon:ClipboardCheck,endpoint:'review/queue',hint:'待確認的翻譯記憶與修正',native:'/admin#/review'},
 {id:'staging',label:'暫存區',icon:Inbox,endpoint:'staging',hint:'修正進入正式資料前的關卡',native:'/admin/staging'},
 {id:'glossary',label:'詞彙表',icon:BookA,endpoint:'glossary/terms',hint:'中英／中日鎖定詞',native:'/admin#/glossary'},
 {id:'tm',label:'翻譯記憶',icon:Database,endpoint:'tm',hint:'已確認的句對',native:'/admin#/tm'},
 {id:'search',label:'搜尋',icon:Search,endpoint:'search',hint:'全文搜尋原文與譯文',native:'/admin#/search'},
 {id:'exports',label:'匯出',icon:FileDown,endpoint:'exports',hint:'SRT／VTT／TXT 檔案',native:'/admin#/exports'},
 {id:'config',label:'設定',icon:Settings2,endpoint:'config',hint:'翻譯設定與預設語言',native:'/admin#/config'},
 {id:'audit',label:'稽核',icon:ScrollText,endpoint:'audit',hint:'誰在何時做了什麼',native:'/admin#/audit'},
 {id:'users',label:'使用者',icon:UserCog,endpoint:'users',hint:'角色與權杖',native:'/admin#/users'},
];
const ADMIN_ORIGIN='http://127.0.0.1:8791';

function fmt(v:unknown):string{
 if(v==null)return '—';
 if(typeof v==='number'){if(v>1e9&&v<4e9)return new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date(v*1000));return Number.isInteger(v)?String(v):v.toFixed(2);}
 if(typeof v==='boolean')return v?'是':'否';
 if(typeof v==='object')return JSON.stringify(v).slice(0,120);
 return String(v).slice(0,160);
}
function firstRows(data:unknown):{name:string;rows:Record<string,unknown>[]}[]{
 if(Array.isArray(data))return [{name:'',rows:data as Record<string,unknown>[]}];
 if(!data||typeof data!=='object')return [];
 return Object.entries(data as Record<string,unknown>).filter(([,v])=>Array.isArray(v)&&(v as unknown[]).every(x=>x&&typeof x==='object')).map(([k,v])=>({name:k,rows:v as Record<string,unknown>[]}));
}
function scalars(data:unknown):[string,unknown][]{
 if(!data||typeof data!=='object'||Array.isArray(data))return [];
 return Object.entries(data as Record<string,unknown>).flatMap(([k,v])=>v&&typeof v==='object'&&!Array.isArray(v)?Object.entries(v as Record<string,unknown>).filter(([,x])=>x==null||typeof x!=='object').map(([kk,x])=>[k+'.'+kk,x] as [string,unknown]):Array.isArray(v)?[]:[[k,v] as [string,unknown]]).slice(0,24);
}
const LABELS:Record<string,string>={tm_pending:'待確認翻譯記憶',staging_pending:'暫存區待審',items:'清單',events:'事件',jobs:'工作',segments:'段落'};

function Table({rows,actions,name=''}:{rows:Record<string,unknown>[];actions?:(row:Record<string,unknown>,table:string)=>React.ReactNode;name?:string}){
 if(!rows.length)return <p className="fine-note">目前沒有資料。</p>;
 const cols=[...new Set(rows.slice(0,20).flatMap(r=>Object.keys(r)))].filter(c=>!['etag','timeline'].includes(c)).slice(0,7);
 return <div className="admin-table-wrap"><table className="admin-table"><thead><tr>{cols.map(c=><th key={c}>{c}</th>)}{actions&&<th>操作</th>}</tr></thead><tbody>{rows.slice(0,100).map((r,i)=><tr key={String(r.id??i)}>{cols.map(c=><td key={c} lang={c.includes('ja')||r.tgt_lang==='ja'&&c.startsWith('tgt')?'ja':undefined}>{fmt(r[c])}</td>)}{actions&&<td className="admin-actions">{actions(r,name)}</td>}</tr>)}</tbody></table></div>;
}

export default function AdminPanel({demoDefault=false}:{demoDefault?:boolean}){
 const [page,setPage]=useState('overview'),[data,setData]=useState<unknown>(null),[extra,setExtra]=useState<Record<string,unknown>>({}),[error,setError]=useState(''),[needsToken,setNeedsToken]=useState(false),[loading,setLoading]=useState(false),[demo,setDemo]=useState(demoDefault),[q,setQ]=useState(''),[searchLang,setSearchLang]=useState('zh');
 const current=ADMIN_PAGES.find(p=>p.id===page)??ADMIN_PAGES[0];
 const get=useCallback(async(path:string)=>{const r=await fetch('/api/breeze-admin/'+path,{cache:'no-store'});const body=await r.json().catch(()=>({})) as {error?:string;detail?:string;needsToken?:boolean};if(!r.ok){const e=new Error(body.error||body.detail||'後台回應 '+r.status) as Error&{needsToken?:boolean};e.needsToken=!!body.needsToken||r.status===401;throw e;}return body as unknown;},[]);
 const load=useCallback(async()=>{
  setLoading(true);setError('');setNeedsToken(false);
  const path=current.id==='search'?(q.trim()?'search?q='+encodeURIComponent(q.trim())+'&lang='+searchLang:''):current.endpoint;
  if(demo){setData(ADMIN_DEMO[current.id==='review'?'review/queue':current.id==='perf'?'perf/metrics':current.id==='glossary'?'glossary/terms':current.endpoint]??null);setExtra(current.id==='overview'?{inventory:ADMIN_DEMO['overview/inventory'],timings:ADMIN_DEMO['overview/asr-timings']}:current.id==='perf'?{timings:ADMIN_DEMO['overview/asr-timings']}:{});setLoading(false);return;}
  if(!path){setData(null);setLoading(false);return;}
  try{setData(await get(path));
   if(current.id==='overview'){const [inventory,timings]=await Promise.allSettled([get('overview/inventory'),get('overview/asr-timings')]);setExtra({inventory:inventory.status==='fulfilled'?inventory.value:null,timings:timings.status==='fulfilled'?timings.value:null});}
   else if(current.id==='perf'){const t=await get('overview/asr-timings').catch(()=>null);setExtra({timings:t});}
   else if(current.id==='glossary'){const p=await get('glossary/proposals').catch(()=>null);setExtra({proposals:p});}
   else setExtra({});
  }catch(e){setData(null);setError(e instanceof Error?e.message:'讀取失敗');setNeedsToken(!!(e as {needsToken?:boolean}).needsToken);}
  finally{setLoading(false);}
 },[current,demo,get,q,searchLang]);
 useEffect(()=>{queueMicrotask(()=>void load());},[load]);
 async function act(path:string,label:string,etag?:unknown){
  if(demo){toast.info('示範模式：「'+label+'」不會送出。');return;}
  try{const r=await fetch('/api/breeze-admin/'+path,{method:'POST',headers:{'Content-Type':'application/json',...(typeof etag==='string'&&etag?{'If-Match':etag}:{})},body:'{}'});const b=await r.json().catch(()=>({})) as {error?:string;detail?:string};if(!r.ok)throw new Error(b.error||b.detail||'失敗 '+r.status);toast.success(label+'完成');void load();}
  catch(e){toast.error(e instanceof Error?e.message:'操作失敗');}
 }
 const ov=(data??{}) as {rtf?:number;rtf_color?:string;asr_gpu?:GpuStatus;components?:Record<string,unknown>;live?:Record<string,unknown>;stale?:boolean};
 const actionsFor=(row:Record<string,unknown>,table:string)=>{
  if(page==='review'||page==='tm'||page==='staging'){const id=row.id;if(id==null)return null;
   // review/queue mixes two id spaces: tm_pending -> /tm/{id}, staging_pending -> /staging/{id} (+ If-Match ETag).
   const isStaging=page==='staging'||table==='staging_pending';
   if(isStaging&&row.state&&row.state!=='pending')return null;
   return <><button onClick={()=>void act((isStaging?'staging/':'tm/')+id+'/approve','核准',isStaging?row.etag:undefined)} aria-label="核准"><Check size={14}/></button><button onClick={()=>void act((isStaging?'staging/':'tm/')+id+'/reject','退回',isStaging?row.etag:undefined)} aria-label="退回"><X size={14}/></button></>;}
  if(page==='glossary'&&row.status==='proposed')return <><button onClick={()=>void act('glossary/proposals/'+row.id+'/approve','核准詞條')}><Check size={14}/></button><button onClick={()=>void act('glossary/proposals/'+row.id+'/reject','退回詞條')}><X size={14}/></button></>;
  if(page==='sessions'&&row.status==='live')return <button onClick={()=>void act('sessions/'+row.id+'/close','關閉場次')}>關閉</button>;
  if(page==='exports'&&row.name&&!demo)return <a href={ADMIN_ORIGIN+'/admin/api/v1/exports/file/'+encodeURIComponent(String(row.name))} target="_blank" rel="noreferrer">下載</a>;
  return null;
 };
 const tables=firstRows(data);
 return <div className="breeze-admin">
  <section className="collection-paper admin-shell">
   <div className="section-heading"><div className="breeze-brand"><ZenShiMark size={42}/><div><h2>Breeze 管理後台</h2><p>原本在 127.0.0.1:8791 的後台，全部收進龜龜的小冊裡。</p></div></div>
    <div className="button-group"><label className="breeze-demo-toggle"><input type="checkbox" checked={demo} onChange={e=>setDemo(e.target.checked)}/> <FlaskConical size={14}/>示範資料</label><Button variant="ghost" onClick={()=>void load()} disabled={loading}><RefreshCw size={15}/>重新整理</Button><Button variant="outline" onClick={()=>window.open(ADMIN_ORIGIN+current.native,'_blank','noopener')}><ExternalLink size={15}/>在原後台開啟</Button></div></div>
   <nav className="admin-subnav" aria-label="後台頁面">{ADMIN_PAGES.map(p=>{const Icon=p.icon;return <button key={p.id} className={page===p.id?'selected':''} aria-current={page===p.id?'page':undefined} onClick={()=>setPage(p.id)}><Icon size={15}/><span>{p.label}</span></button>;})}</nav>
   {demo&&<p className="breeze-demo-note" role="note">示範資料（假資料，未連線後台）：用來預覽每一頁的版面。</p>}
   <div className="admin-page-head"><span className="stamp">{current.label}</span><p>{current.hint}</p></div>
   {page==='search'&&<form className="admin-search" onSubmit={e=>{e.preventDefault();void load();}}><Input value={q} onChange={e=>setQ(e.target.value)} placeholder="搜尋中文原文、英文或日文譯文" aria-label="搜尋"/><select className="breeze-input" value={searchLang} onChange={e=>setSearchLang(e.target.value)} aria-label="搜尋語言"><option value="zh">中文</option><option value="en">英文</option><option value="ja">日文</option></select><Button type="submit"><Search size={15}/>搜尋</Button></form>}
   {error?<div className="breeze-offline" role="status">{needsToken?<KeyRound size={16}/>:null}<div><strong>{error}</strong>{needsToken&&<p>在主持電腦設定環境變數 <code>BREEZE_ADMIN_TOKEN</code>（後台 CLI 權杖，或在「使用者」頁為自己產生的 API 權杖）後重新啟動禪譯；或勾選「示範資料」預覽。</p>}</div></div>:loading&&!data?<p className="fine-note">讀取中…</p>:null}
   {page==='overview'&&data!=null&&<div className="admin-overview">
    <article className="admin-card asr-card"><h3>ASR 加速</h3><AsrChip gpu={ov.asr_gpu}/><p>即時 RTF <strong className={'rtf rtf-'+(ov.rtf_color||'grey')}>{ov.rtf!=null?ov.rtf.toFixed(2):'—'}</strong>{ov.stale?'（資料過時）':''}</p></article>
    <article className="admin-card"><h3>元件狀態</h3><ul className="admin-kv">{Object.entries(ov.components||{}).map(([k,v])=>{const st=v&&typeof v==='object'&&'status' in v?String((v as {status:unknown}).status):String(v);return <li key={k}><span>{k}</span><b className={/up|ok|idle|ready/.test(st)?'ok':st==='unknown'?'':'warn'}>{st}</b></li>;})}</ul></article>
    <article className="admin-card"><h3>即時</h3><ul className="admin-kv">{Object.entries(ov.live||{}).filter(([,v])=>typeof v!=='object').slice(0,8).map(([k,v])=><li key={k}><span>{k}</span><b>{fmt(v)}</b></li>)}</ul></article>
    {extra.inventory?<article className="admin-card"><h3>資料庫存量</h3><ul className="admin-kv">{scalars(extra.inventory).map(([k,v])=><li key={k}><span>{k.replace('counts.','')}</span><b>{fmt(v)}</b></li>)}</ul></article>:null}
    {extra.timings?<article className="admin-card"><h3>ASR 延遲</h3><ul className="admin-kv">{scalars(extra.timings).map(([k,v])=><li key={k}><span>{k}</span><b>{fmt(v)}</b></li>)}</ul></article>:null}
   </div>}
   {page==='perf'&&data!=null&&<div className="admin-overview">{[['效能指標',data],['ASR 延遲',extra.timings]].filter(([,d])=>d).map(([t,d])=><article className="admin-card" key={String(t)}><h3>{String(t)}</h3><ul className="admin-kv">{scalars(d).map(([k,v])=><li key={k}><span>{k}</span><b>{fmt(v)}</b></li>)}</ul></article>)}<article className="admin-card"><h3>完整效能頁</h3><p>圖表與趨勢在原效能頁。</p><Button variant="outline" onClick={()=>window.open(ADMIN_ORIGIN+'/admin/perf','_blank','noopener')}><ExternalLink size={15}/>開啟 /admin/perf</Button></article></div>}
   {page!=='overview'&&page!=='perf'&&data!=null&&<>
    {tables.length?tables.map(t=><div key={t.name} className="admin-section">{t.name&&<h3>{LABELS[t.name]||t.name} <span className="count">{t.rows.length}</span></h3>}<Table rows={t.rows} actions={actionsFor} name={t.name}/></div>):<ul className="admin-kv wide">{scalars(data).map(([k,v])=><li key={k}><span>{k}</span><b>{fmt(v)}</b></li>)}</ul>}
    {page==='glossary'&&extra.proposals?<div className="admin-section"><h3>待審詞條</h3><Table rows={firstRows(extra.proposals)[0]?.rows||[]} actions={r=><><button onClick={()=>void act('glossary/proposals/'+r.id+'/approve','核准詞條')}><Check size={14}/></button><button onClick={()=>void act('glossary/proposals/'+r.id+'/reject','退回詞條')}><X size={14}/></button></>}/></div>:null}
   </>}
   <p className="fine-note">後台資料經禪譯伺服器以權杖轉送；寫入只開放審核、核准、關閉場次等後台本身就有的操作。刪除、使用者權杖與設定修改請在原後台進行。</p>
  </section>
 </div>;
}
