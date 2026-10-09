export const dynamic='force-dynamic';
import { dictionarySources,lookupDictionary,importPersonalDictionary } from '@/lib/dictionary';
import { breezeHostAllowed } from '@/lib/breeze-host';
import { getEnv } from '@/lib/env';
import { failure } from '@/lib/server';

export async function GET(request:Request){
 try {const env=getEnv(),allowed=!!env.BREEZE_AGENT_TOKEN&&breezeHostAllowed(request,env),query=new URL(request.url).searchParams.get('q')??'';return Response.json({sources:await dictionarySources(allowed),entries:query?await lookupDictionary(query,allowed):[],privateAllowed:allowed});}catch(error){return failure(error);}
}
export async function POST(request:Request){
 try {
  const env=getEnv();if(!env.BREEZE_AGENT_TOKEN||!breezeHostAllowed(request,env))return Response.json({error:'請從已配對的主持 App 匯入自有字典。'},{status:403});
  if(Number(request.headers.get('content-length')||0)>2_000_000)return Response.json({error:'每次匯入請小於 2 MB。'},{status:413});
  const reader=request.body?.getReader();if(!reader)return Response.json({error:'請提供詞典 JSON。'},{status:400});
  const chunks:Uint8Array[]=[];let size=0;
  try {for(;;){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>2_000_000){await reader.cancel();return Response.json({error:'每次匯入請小於 2 MB。'},{status:413});}chunks.push(value);}}finally{reader.releaseLock();}
  const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
  const text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);
  return Response.json(await importPersonalDictionary(JSON.parse(text)));
 }catch(error){if(error instanceof SyntaxError||error instanceof TypeError)return Response.json({error:'請提供有效的 UTF-8 JSON 詞典。'},{status:400});return failure(error);}
}
