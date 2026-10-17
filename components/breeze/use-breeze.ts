'use client';
import {useCallback,useEffect,useRef,useState} from 'react';
import type {GpuStatus,TargetLang} from '@/lib/breeze-live';

export type BreezeSetup={room:string;listen_url:string|null;listen_url_name?:string|null;listen_key?:string;share_ready:boolean;share_message?:string|null;port:number;asr_mode?:string;asr_ready?:boolean;asr_degraded?:boolean;asr_gpu?:GpuStatus;translate_configured?:boolean;translate_label?:string;queue?:Record<string,unknown>;listeners?:number;draft_available?:boolean;tgt_lang?:TargetLang;storage?:boolean;resident_error?:string};

export async function breezeJson<T>(path:string,init?:RequestInit):Promise<T>{
 const r=await fetch('/api/breeze/'+path,{cache:'no-store',...init});
 const body=await r.json().catch(()=>({})) as {error?:string;detail?:string}&T;
 if(!r.ok)throw new Error(body.error||(typeof body.detail==='string'?body.detail:'')||'Breeze 回應 '+r.status);
 return body;
}
export function postBreeze<T>(path:string,payload:unknown){return breezeJson<T>(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});}

/** Poll /api/setup through the turtle server; null while offline. */
export function useBreezeSetup(room:string,enabled=true,everyMs=5000){
 const [setup,setSetup]=useState<BreezeSetup|null>(null),[offline,setOffline]=useState(false),[checked,setChecked]=useState(false);
 const alive=useRef(true);
 const refresh=useCallback(async()=>{try{const s=await breezeJson<BreezeSetup>('setup?room_id='+encodeURIComponent(room));if(alive.current){setSetup(s);setOffline(false);}}catch{if(alive.current){setSetup(null);setOffline(true);}}finally{if(alive.current)setChecked(true);}},[room]);
 useEffect(()=>{alive.current=true;if(!enabled)return()=>{alive.current=false;};queueMicrotask(()=>void refresh());const t=setInterval(()=>void refresh(),everyMs);return()=>{alive.current=false;clearInterval(t);};},[refresh,enabled,everyMs]);
 return {setup,offline,checked,refresh};
}
