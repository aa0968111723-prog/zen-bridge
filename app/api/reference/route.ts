export const dynamic='force-dynamic';
import {bindings,one,write,failure,id} from '@/lib/server';
export async function POST(r:Request){try{
 if(Number(r.headers.get('content-length')??0)>2_000_000)throw new Error('聲音樣本需小於 1 MB。');
 const f=await r.formData(),file=f.get('audio'),personId=String(f.get('personId')??''),duration=Number(f.get('duration'));
 if(!(file instanceof File)||file.size>1_000_000||!file.size||duration<2||duration>10||!Number.isFinite(duration))throw new Error('請上傳 2–10 秒、1 MB 以內的聲音樣本。');
 if(!/^(audio\/(wav|x-wav|mpeg|mp3|mp4|x-m4a|webm|ogg)|video\/webm)$/.test(file.type))throw new Error('請使用 WAV、MP3、M4A、WebM 或 Ogg 聲音檔。');
 if(!await one('SELECT id FROM people WHERE id=?',personId))throw new Error('找不到講者。');
 const bucket=bindings().BUCKET;if(!bucket)throw new Error('檔案儲存尚未就緒。');
 const old=await one<{reference_key:string|null}>('SELECT reference_key FROM people WHERE id=?',personId),key='references/'+personId+'/'+id();
 const mime=file.type==='audio/x-m4a'?'audio/mp4':file.type;
 await bucket.put(key,await file.arrayBuffer(),{httpMetadata:{contentType:mime}});
 try{await write('UPDATE people SET reference_key=?,reference_type=? WHERE id=?',key,mime,personId);}catch(e){await bucket.delete(key);throw e;}
 if(old?.reference_key)await bucket.delete(old.reference_key);
 return Response.json({ok:true});
 }catch(e){return failure(e);}}
export async function GET(r:Request){try{
 const person=await one<{reference_key:string|null}>('SELECT reference_key FROM people WHERE id=?',new URL(r.url).searchParams.get('id'));
 const object=person?.reference_key?await bindings().BUCKET?.get(person.reference_key):null;
 if(!object)return Response.json({error:'尚未加入聲音樣本。'},{status:404});
 return new Response(object.body,{headers:{'Content-Type':object.httpMetadata?.contentType??'audio/wav','Content-Length':String(object.size),'Cache-Control':'private, no-store'}});
 }catch(e){return failure(e);}}
