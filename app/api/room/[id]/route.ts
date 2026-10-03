import QRCode from 'qrcode';
import {getEnv} from '@/lib/env';
import {roomHistory,roomUrl} from '@/lib/rooms';

export const dynamic='force-dynamic';

export async function GET(_request:Request,{params}:{params:Promise<{id:string}>}){
 const {id}=await params,env=getEnv();
 if(env.SHARE!=='room'||!env.PUBLIC_BASE_URL)return Response.json({error:'聽眾房未啟用：請設定 PUBLIC_BASE_URL。'},{status:503});
 const qr_url=roomUrl(env.PUBLIC_BASE_URL,id);
 return Response.json({id,qr_url,qr_data_url:await QRCode.toDataURL(qr_url,{margin:1,width:320}),history:roomHistory(id)});
}
