export const dynamic='force-dynamic';
import {getEnv} from '@/lib/env';
import {breezeHostAllowed} from '@/lib/breeze-host';
import {desktopAgent} from '@/lib/desktop-agent';
import {rows,failure} from '@/lib/server';
export async function GET(request:Request){
 try{
  const env=getEnv();if(!env.BREEZE_AGENT_TOKEN||!breezeHostAllowed(request,env))return Response.json({error:'操作台需要已配對主持 App。'},{status:403});
  const counts:Record<string,number>={};for(const table of ['people','sessions','segments','memories']){const result=await rows<{count:number}>(`SELECT count(*) AS count FROM ${table}`);counts[table]=Number(result[0]?.count??0);}
  let transport;try{transport=await desktopAgent('/health');}catch{transport={connected:false};}
  return Response.json({counts,transport},{headers:{'Cache-Control':'no-store'}});
 }catch(e){return failure(e);}
}
