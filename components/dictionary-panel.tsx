'use client';
import {useEffect,useState} from 'react';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import type {DictionaryEntry,DictionarySource} from '@/lib/dictionary';

type Result={sources:DictionarySource[];entries:DictionaryEntry[];privateAllowed:boolean};
const definition=(value:string)=>value.replace(/\\n/g,'\n');
export default function DictionaryPanel(){
 const [query,setQuery]=useState(''),[result,setResult]=useState<Result>({sources:[],entries:[],privateAllowed:false}),[busy,setBusy]=useState(false),[error,setError]=useState('');
 async function load(q=''){setBusy(true);setError('');try{const r=await fetch('/api/dictionaries?q='+encodeURIComponent(q));const body=await r.json() as Result & {error?:string};if(!r.ok)throw Error(body.error||'詞典讀取失敗');setResult(body);}catch(e){setError(e instanceof Error?e.message:'詞典讀取失敗');}finally{setBusy(false);}}
 useEffect(()=>{void load();},[]);
 async function upload(file:File){
  setError('');if(file.size>2_000_000){setError('每次匯入請小於 2 MB。');return;}setBusy(true);
  try{const parsed=JSON.parse(await file.text());const payload=Array.isArray(parsed)?{title:file.name,entries:parsed}:parsed;const r=await fetch('/api/dictionaries',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const body=await r.json() as Result & {error?:string};if(!r.ok)throw Error(body.error||'匯入失敗');await load(query);}catch(e){setError(e instanceof Error?e.message:'匯入失敗');}finally{setBusy(false);}
 }
 return <section className="collection-paper memory-ledger"><div className="section-heading"><div><h2>中英詞典</h2><p>查字詞、簡繁詞頭、英文詞形與釋義。詞典參考保留來源，翻譯時優先採用你的已確認筆記。</p></div></div>
  <form className="memory-toolbar" onSubmit={e=>{e.preventDefault();void load(query);}}><label className="search-field"><Input aria-label="搜尋中英詞典" placeholder="例如：禪、meditation、breathing" value={query} onChange={e=>setQuery(e.target.value)} maxLength={160}/></label><Button disabled={busy}>搜尋</Button></form>
  {error&&<p role="alert">{error}</p>}
  {!result.sources.length&&<p>詞典資料尚在準備中；可重新搜尋確認載入狀態。</p>}
  <div className="memory-entries">{result.entries.map(entry=><article className="memory-entry" key={entry.id}><div className="entry-body"><h3>{entry.word}{entry.alternative&&entry.alternative!==entry.word?' · '+entry.alternative:''}</h3>{entry.pronunciation&&<p>{entry.pronunciation}</p>}{entry.zh&&<p style={{whiteSpace:'pre-wrap'}}>{definition(entry.zh)}</p>}{entry.en&&<p className="entry-english" lang="en" style={{whiteSpace:'pre-wrap'}}>{definition(entry.en)}</p>}<p className="fine-note">{entry.source_title} · {entry.license}{entry.source_url&&<> · <a href={entry.source_url} target="_blank" rel="noreferrer">來源</a></>}</p></div></article>)}</div>
  {query&&!busy&&!result.entries.length&&<p>沒有找到這個詞頭。可試簡繁寫法、較短字詞或英文原形。</p>}
  <div className="section-heading"><h3>收錄來源</h3></div><ul>{result.sources.map(source=><li key={source.id}>{source.title} · {Number(source.entry_count).toLocaleString()} 條 · {source.license}{source.license_url&&<> · <a href={source.license_url} target="_blank" rel="noreferrer">授權</a></>}{!source.public?' · 主持機自有字典':''}</li>)}</ul>
  {result.privateAllowed&&<div><h3>匯入自有字典</h3><p>上傳 UTF-8 JSON，內容為詞條陣列：word、alternative、pronunciation、zh、en。每次最多 5,000 條、2 MB。自有字典只供已配對主持機查詢與翻譯參考。</p><input type="file" aria-label="匯入自有字典 JSON" accept=".json,application/json" disabled={busy} onChange={e=>{const f=e.target.files?.[0];if(f)void upload(f);e.target.value='';}}/></div>}
  <p className="fine-note">收錄開放授權詞典與你有權使用的資料。字典無法取代句子語境；多義詞仍需核對原意。</p>
 </section>;
}
