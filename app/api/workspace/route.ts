export const dynamic='force-dynamic';
import {workspace,mutate,failure} from '@/lib/server';
export async function GET(r:Request){try{return Response.json(await workspace(new URL(r.url).searchParams.get('session')),{headers:{'Cache-Control':'no-store'}});}catch(e){return failure(e);}}
export async function POST(r:Request){try{if(Number(r.headers.get('content-length')??0)>200000)return Response.json({error:'資料太長。'},{status:413});return Response.json(await mutate(await r.json()));}catch(e){return failure(e);}}