export const dynamic='force-dynamic';
import {workspace,mutate,failure} from '@/lib/server';
import {getEnv} from '@/lib/env';
import {breezeHostAllowed} from '@/lib/breeze-host';
export async function GET(r:Request){try{return Response.json(await workspace(new URL(r.url).searchParams.get('session'),breezeHostAllowed(r,getEnv())),{headers:{'Cache-Control':'no-store'}});}catch(e){return failure(e);}}
export async function POST(r:Request){try{if(Number(r.headers.get('content-length')??0)>200000)return Response.json({error:'資料太長。'},{status:413});return Response.json(await mutate(await r.json(),!!getEnv().BREEZE_AGENT_TOKEN&&breezeHostAllowed(r,getEnv())));}catch(e){return failure(e);}}
