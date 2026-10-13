/** Local PCM backup, independent of ASR and cloud availability. Never uploads audio. */
export type RecordingInfo={id:string;sessionId:string;label:string;startedAt:number;updatedAt:number;bytes:number;chunks:number;state:'recording'|'complete'|'error'};
type Piece={recordingId:string;seq:number;wav:Blob};
const LIMIT=512*1024*1024;
function openArchive():Promise<IDBDatabase>{
 return new Promise((resolve,reject)=>{
  if(typeof indexedDB==='undefined'){reject(Error('這個環境不支援本機錄音儲存。'));return;}
  const request=indexedDB.open('zen-recording-archive',1);
  let blocked=false;
  request.onupgradeneeded=()=>{const db=request.result;db.createObjectStore('recordings',{keyPath:'id'});db.createObjectStore('pieces',{keyPath:['recordingId','seq']}).createIndex('recordingId','recordingId');};
  request.onsuccess=()=>{const db=request.result;if(blocked){db.close();return;}db.onversionchange=()=>db.close();resolve(db);};
  request.onerror=()=>reject(Error('本機錄音儲存無法開啟。'));
  request.onblocked=()=>{blocked=true;reject(Error('請先關閉其他舊版禪譯視窗，再開啟錄音。'));};
 });
}
function transact<T>(db:IDBDatabase,names:string[],mode:IDBTransactionMode,run:(tx:IDBTransaction,set:(value:T)=>void)=>void):Promise<T>{
 return new Promise((resolve,reject)=>{const tx=db.transaction(names,mode);let value:T;tx.oncomplete=()=>resolve(value);tx.onabort=()=>reject(Error('本機錄音寫入失敗，請檢查磁碟空間。'));tx.onerror=()=>{};try{run(tx,v=>{value=v;});}catch(error){tx.abort();reject(error);}});
}
export async function createRecording(sessionId:string,label:string,onChange:()=>void,onError:()=>void){
 const db=await openArchive();
 let info:RecordingInfo={id:crypto.randomUUID(),sessionId,label:label.slice(0,160),startedAt:Date.now(),updatedAt:Date.now(),bytes:0,chunks:0,state:'recording'};
 try{await transact(db,['recordings'],'readwrite',(tx,set)=>{tx.objectStore('recordings').add(info);set(undefined);});}catch(error){db.close();throw error;}
 let queue:Promise<void>=Promise.resolve(),pending=0,failed=false,finished=false;
 const fail=()=>{if(!failed){failed=true;onError();}};
 return {
  append(wav:Blob){
   if(finished||failed)return;
   if(pending>=4){fail();return;} // At most 20 seconds of uncommitted audio.
   pending++;
   queue=queue.then(async()=>{
    if(wav.size<44||info.bytes+wav.size-44>LIMIT)throw Error('錄音儲存已達上限。');
    const next={...info,updatedAt:Date.now(),chunks:info.chunks+1,bytes:info.bytes+wav.size-44};
    await transact(db,['recordings','pieces'],'readwrite',(tx,set)=>{tx.objectStore('pieces').add({recordingId:info.id,seq:info.chunks,wav} satisfies Piece);tx.objectStore('recordings').put(next);set(undefined);});
    info=next;onChange();
   }).catch(fail).finally(()=>{pending--;});
  },
  async finish(){
   if(finished)return;finished=true;await queue;
   try{info={...info,state:failed?'error':'complete',updatedAt:Date.now()};await transact(db,['recordings'],'readwrite',(tx,set)=>{tx.objectStore('recordings').put(info);set(undefined);});}
   catch{fail();}finally{db.close();onChange();}
  },
 };
}
export async function listRecordings():Promise<RecordingInfo[]>{
 const db=await openArchive();try{return await transact(db,['recordings'],'readonly',(tx,set)=>{const r=tx.objectStore('recordings').getAll();r.onsuccess=()=>set((r.result as RecordingInfo[]).filter(x=>x.bytes>0).sort((a,b)=>b.startedAt-a.startedAt));});}finally{db.close();}
}
export async function recordingWav(id:string):Promise<Blob>{
 const db=await openArchive();let pieces:Piece[];
 try{pieces=await transact(db,['pieces'],'readonly',(tx,set)=>{const r=tx.objectStore('pieces').index('recordingId').getAll(id);r.onsuccess=()=>set(r.result);});}finally{db.close();}
 pieces.sort((a,b)=>a.seq-b.seq);if(!pieces.length)throw Error('這筆錄音沒有可下載的聲音。');
 const bytes=pieces.reduce((n,p)=>n+p.wav.size-44,0),buffer=new ArrayBuffer(44),v=new DataView(buffer);
 const text=(offset:number,s:string)=>{for(let i=0;i<s.length;i++)v.setUint8(offset+i,s.charCodeAt(i));};
 text(0,'RIFF');v.setUint32(4,bytes+36,true);text(8,'WAVE');text(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,16000,true);v.setUint32(28,32000,true);v.setUint16(32,2,true);v.setUint16(34,16,true);text(36,'data');v.setUint32(40,bytes,true);
 return new Blob([buffer,...pieces.map(p=>p.wav.slice(44))],{type:'audio/wav'});
}
export async function removeRecording(id:string){
 const db=await openArchive();try{await transact(db,['recordings','pieces'],'readwrite',(tx,set)=>{tx.objectStore('recordings').delete(id);const r=tx.objectStore('pieces').index('recordingId').openCursor(id);r.onsuccess=()=>{const cursor=r.result;if(cursor){cursor.delete();cursor.continue();}};set(undefined);});}finally{db.close();}
}
