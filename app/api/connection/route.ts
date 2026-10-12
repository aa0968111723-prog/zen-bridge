export const dynamic='force-dynamic';
import {connectionStatus,failure} from '@/lib/server';
import {getEnv} from '@/lib/env';
import {breezeHostAllowed} from '@/lib/breeze-host';

export async function GET(request:Request){
 try{
  const env=getEnv(),authorized=breezeHostAllowed(request,env);
  if(env.BREEZE_AGENT_TOKEN&&!authorized)return Response.json({error:'請從已配對的主持 App 檢查連線。'},{status:403});
  return Response.json(await connectionStatus(null,authorized),{headers:{'Cache-Control':'no-store'}});
 }catch(error){return failure(error);}
}
