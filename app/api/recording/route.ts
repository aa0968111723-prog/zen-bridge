export const dynamic='force-dynamic';
import {z} from 'zod';
import {bindings,one,failure} from '@/lib/server';
export async function GET(r:Request){try{
 const segmentId=z.string().uuid().parse(new URL(r.url).searchParams.get('id'));
 const segment=await one<{audio_key:string|null}>('SELECT audio_key FROM segments WHERE id=?',segmentId);
 if(!segment?.audio_key)return Response.json({error:'這段紀錄沒有錄音。'},{status:404});
 const object=await bindings().BUCKET?.get(segment.audio_key);
 if(!object)return Response.json({error:'錄音檔案不存在。'},{status:404});
 return new Response(object.body,{headers:{'Content-Type':object.httpMetadata?.contentType??'audio/wav','Content-Length':String(object.size),'Cache-Control':'private, no-store','Content-Disposition':'inline; filename="speech.wav"'}});
 }catch(e){return failure(e);}}
