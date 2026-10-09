'use client';
import {useEffect,useState} from 'react';
import type {RoomEvent} from '@/lib/rooms';

export default function Listener({params}:{params:Promise<{id:string}>}){
 const [id,setId]=useState(''),[lines,setLines]=useState<RoomEvent[]>([]),[connected,setConnected]=useState(false);
 useEffect(()=>{void params.then(value=>setId(value.id));},[params]);
 useEffect(()=>{if(!id)return;let events:EventSource|undefined,socket:WebSocket|undefined;
  const receive=(body:{type:'hello';history?:RoomEvent[]}|RoomEvent)=>{setConnected(true);if(body.type==='hello')setLines(body.history??[]);else if(body.type==='final')setLines(old=>[...old,body].slice(-40));};
  const fallback=()=>{if(events)return;events=new EventSource('/ws/listen?room='+encodeURIComponent(id));events.onmessage=e=>receive(JSON.parse(e.data));events.onerror=()=>setConnected(false);};
  try{socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/listen?room=${encodeURIComponent(id)}`);socket.onmessage=e=>receive(JSON.parse(e.data));socket.onerror=fallback;socket.onclose=fallback;}catch{fallback();}
  return()=>{socket?.close();events?.close();};
 },[id]);
 return <main className="listener-page"><header><h1>禪譯 Zen Bridge</h1><span className={'connection-state '+(connected?'ready':'')}>{connected?'已連線':'重新連線中'}</span></header><section aria-live="polite">{!lines.length?<p>等待主持人送出譯文…</p>:lines.map(line=><article key={line.id}><p className="listener-source">{line.direction==='zh-en'?line.zh:line.en}</p><p className="listener-target" lang={line.direction==='zh-en'?'en':'zh-TW'}>{line.direction==='zh-en'?line.en:line.zh}</p></article>)}</section></main>;
}
