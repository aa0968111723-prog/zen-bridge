export const dynamic='force-dynamic';
import {z} from 'zod';
import {getEnv} from '@/lib/env';
import {breezeHostAllowed} from '@/lib/breeze-host';
import {desktopAgent} from '@/lib/desktop-agent';
import {one,failure} from '@/lib/server';
import type {Session} from '@/lib/domain';

export async function POST(request:Request){
 try{
  const env=getEnv();
  if(!env.BREEZE_AGENT_TOKEN||!breezeHostAllowed(request,env))return Response.json({error:'字幕預覽需要已配對主持 App。'},{status:403});
  if(!request.body)throw Error('缺少預覽資料。');
  const reader=request.body.getReader(),parts:Uint8Array[]=[];let size=0;
  try{while(true){const part=await reader.read();if(part.done)break;size+=part.value.length;if(size>240000){await reader.cancel();return Response.json({error:'預覽資料過大。'},{status:413});}parts.push(part.value);}}finally{reader.releaseLock();}
  const body=new Uint8Array(size);let cursor=0;for(const part of parts){body.set(part,cursor);cursor+=part.length;}
  const form=await new Response(body,{headers:{'Content-Type':request.headers.get('content-type')||''}}).formData();
  const sessionId=z.string().uuid().parse(form.get('sessionId'));
  const direction=z.enum(['zh-en','en-zh','zh-ja']).parse(form.get('direction'));
  const file=form.get('audio');if(!(file instanceof File)||file.size<44||file.size>220000)throw Error('預覽錄音格式不正確。');
  const session=await one<Session>('SELECT * FROM sessions WHERE id=?',sessionId);
  if(!session||session.status==='finished')return Response.json({error:'這場活動已結束或不存在。'},{status:409});
  const result=await desktopAgent('/preview',{audio:Buffer.from(await file.arrayBuffer()).toString('base64'),language:direction==='en-zh'?'en':'zh'},7500) as {source?:unknown;ephemeral?:unknown};
  if(result.ephemeral!==true||typeof result.source!=='string'||result.source.length>8000)throw Error('字幕預覽回應格式不正確。');
  return Response.json({draft:result.source},{headers:{'Cache-Control':'no-store'}});
 }catch(error){return failure(error);}
}
