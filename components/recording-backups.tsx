'use client';
import {useEffect,useState} from 'react';
import {listRecordings,recordingWav,removeRecording,type RecordingInfo} from '@/lib/recording-archive';
import {Button} from '@/components/ui/button';
import {toast} from 'sonner';
export default function RecordingBackups({revision,active}:{revision:number;active:boolean}){
 const [items,setItems]=useState<RecordingInfo[]>([]);
 useEffect(()=>{let closed=false;void listRecordings().then(rows=>{if(!closed)setItems(rows);}).catch(()=>{});return()=>{closed=true;};},[revision]);
 async function download(item:RecordingInfo){try{const wav=await recordingWav(item.id),url=URL.createObjectURL(wav),a=document.createElement('a');a.href=url;a.download='禪譯錄音-'+new Date(item.startedAt).toISOString().replace(/[:.]/g,'-')+'.wav';a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);}catch{toast.error('無法讀取本機錄音。');}}
 async function remove(item:RecordingInfo){if(!confirm('刪除這筆本機原音備份？已下載的檔案不受影響。'))return;try{await removeRecording(item.id);setItems(await listRecordings());}catch{toast.error('錄音刪除失敗。');}}
 return <details className="audio-setup"><summary>本機原音備份 {active?'・正在保存':items.length?'・'+items.length+' 筆':''}</summary><div className="audio-setup-body"><p>原音留在這個 App／瀏覽器，不上傳。字幕處理暫停時仍會保存聲音。請下載備份；清除網站資料或解除安裝可能移除錄音。異常關閉時最後約 5 秒可能尚未寫入。</p>{!items.length&&<p>開始收音後會在這裡顯示可下載的錄音。</p>}{items.map(item=><div key={item.id}><strong>{item.label||'社課錄音'}</strong><p>{new Date(item.startedAt).toLocaleString('zh-TW')} ・ {Math.floor(item.bytes/32000)} 秒 ・ {item.state==='complete'?'已保存':item.state==='error'?'保存中斷，已寫入部分可下載':'尚未結束，已寫入部分可下載'}</p><Button variant="outline" onClick={()=>void download(item)}>下載 WAV</Button><Button variant="ghost" disabled={active} onClick={()=>void remove(item)}>刪除本機備份</Button></div>)}</div></details>;
}
