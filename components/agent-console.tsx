'use client';
import {useEffect,useState} from 'react';
import {Button} from '@/components/ui/button';
type Event={kind:string;elapsed_ms:number;ok:boolean;at:number;audio_seconds?:number;rtf?:number;route?:string};
type Overview={counts:Record<string,number>;transport:{connected:boolean;busy?:boolean;waiting?:number;control_pending?:number;agent?:{capabilities?:string[];metrics?:{asr_ready:boolean;metrics:{uptime_s:number;completed:number;failed:number;history:Event[]};translation:{local_model:string;cloud_model:string;hermes_auth_configured:boolean;last?:{route:string;elapsed_ms:number}}}}}};
export default function AgentConsole(){
 const [data,setData]=useState<Overview|null>(null),[error,setError]=useState(''),[mode,setMode]=useState('');
 async function refresh(){try{const r=await fetch('/api/agent');const body=await r.json() as Overview&{error?:string};if(!r.ok)throw Error(body.error||'操作台尚未連接');setData(body);setError('');}catch(e){setError(e instanceof Error?e.message:'操作台尚未連接');}}
 useEffect(()=>{const stored=localStorage.getItem('zen-agent-translation-mode');if(stored&&['local','cloud','hybrid'].includes(stored))setMode(stored);void refresh();const timer=setInterval(()=>void refresh(),5000);return()=>clearInterval(timer);},[]);
 const telemetry=data?.transport.agent?.metrics,metrics=telemetry?.metrics;
 return <section className="collection-paper"><div className="section-heading"><div><h2>AI 翻譯代理操作台</h2><p>檢視資料總量、電腦代理與每段處理耗時。</p></div><Button variant="outline" onClick={()=>void refresh()}>更新狀態</Button></div>
  {error&&<p role="alert">{error}</p>}
  <div className="setting-row"><div><h3>翻譯處理模式</h3><p>本機模式使用電腦模型；雲端模式經電腦版 Hermes；混合模式在本機服務失敗時改用 Hermes。</p></div><select aria-label="AI 翻譯代理模式" disabled={!data?.transport.agent?.capabilities?.includes('translate')} value={mode} onChange={e=>{setMode(e.target.value);if(e.target.value)localStorage.setItem('zen-agent-translation-mode',e.target.value);else localStorage.removeItem('zen-agent-translation-mode');window.dispatchEvent(new Event('zen-agent-mode'));}}><option value="">沿用既有服務</option><option value="local">電腦本機模型</option><option value="cloud">電腦 Hermes 雲端連線</option><option value="hybrid">本機優先／Hermes 備援</option></select></div>
  <p>主持電腦：{data?.transport.connected?'已連線':'尚未連線'} · 辨識：{data?.transport.busy?'處理中':'待命'} · 等待：{data?.transport.waiting??0} · 翻譯工作：{data?.transport.control_pending??0}</p>
  {!data?.transport.agent?.capabilities?.includes('translate')&&<p>目前 App 尚未提供桌面翻譯代理能力；請更新後再切換本機或混合模式。</p>}
  <div className="section-heading"><h3>資料總覽</h3></div><ul>{Object.entries(data?.counts??{}).map(([name,count])=><li key={name}>{{people:'講者',sessions:'課程',segments:'字幕',memories:'翻譯筆記'}[name as 'people']||name}：{count.toLocaleString()}</li>)}</ul>
  <p>本機模型：{telemetry?.translation.local_model||'尚未回報'} · Hermes 模型：{telemetry?.translation.cloud_model||'尚未回報'} · Hermes 認證：{telemetry?.translation.hermes_auth_configured?'已設定':'尚未設定'}</p>
  <p>已完成：{metrics?.completed??0} · 失敗：{metrics?.failed??0} · 代理運作：{metrics?.uptime_s??0} 秒</p>
  <h3>最近處理記錄</h3><div style={{overflowX:'auto'}}><table><thead><tr><th>時間</th><th>工作</th><th>路徑</th><th>音訊秒數</th><th>耗時 ms</th><th>RTF</th><th>結果</th></tr></thead><tbody>{[...(metrics?.history??[])].reverse().map((e,i)=><tr key={e.at+'-'+i}><td>{new Date(e.at*1000).toLocaleTimeString('zh-TW')}</td><td>{e.kind==='transcribe'?'辨識':'翻譯'}</td><td>{e.route||'本機辨識'}</td><td>{e.audio_seconds??'—'}</td><td>{e.elapsed_ms}</td><td>{e.rtf??'—'}</td><td>{e.ok?'完成':'失敗'}</td></tr>)}</tbody></table></div>
  <p className="fine-note">RTF 小於 1 表示處理快於該段語音；此處顯示實測值。記錄不包含聲音內容或連線密鑰。完整課程與字幕資料可從既有匯出功能下載。</p>
 </section>;
}
