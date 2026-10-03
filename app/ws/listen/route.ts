import {roomHistory,subscribeRoom,type RoomEvent} from '@/lib/rooms';
import {rows} from '@/lib/server';
import type {Segment} from '@/lib/domain';

export const dynamic = 'force-dynamic';

async function persistedHistory(roomId:string){
  try{
    const segments=(await rows<Segment>('SELECT * FROM segments WHERE session_id=? ORDER BY created_at DESC,"offset" DESC LIMIT 40',roomId)).reverse();
    return segments.map(segment=>({type:'final' as const,id:segment.id,zh:segment.zh,en:segment.en,direction:segment.direction,t:Date.parse(segment.created_at)||0}));
  }catch{return roomHistory(roomId);}
}

export async function GET(request: Request) {
  const roomId = new URL(request.url).searchParams.get('room')?.slice(0, 80) || '';
  if (!roomId) return Response.json({error:'缺少 room。'},{status:400});
  const history=await persistedHistory(roomId),seen=new Set(history.map(event=>event.id));
  const pairConstructor = (globalThis as unknown as {WebSocketPair?:new()=>{0:WebSocket;1:WebSocket}}).WebSocketPair;
  if (request.headers.get('upgrade')?.toLowerCase()==='websocket'&&pairConstructor) {
    const pair=new pairConstructor(),client=pair[0],server=pair[1] as WebSocket&{accept():void};
    server.accept();
    server.send(JSON.stringify({type:'hello',role:'listen',history}));
    const send=(event:RoomEvent)=>{if(!seen.has(event.id)){seen.add(event.id);server.send(JSON.stringify(event));}};
    const unsubscribe=subscribeRoom(roomId,send);
    const poll=setInterval(()=>{void persistedHistory(roomId).then(events=>events.forEach(send));},1000);
    server.addEventListener('close',()=>{clearInterval(poll);unsubscribe();},{once:true});
    return new Response(null,{status:101,webSocket:client} as ResponseInit&{webSocket:WebSocket});
  }
  const encoder=new TextEncoder();
  let unsubscribe=()=>{},poll:ReturnType<typeof setInterval>|undefined;
  const stream=new ReadableStream({
    start(controller){
      const send=(event:RoomEvent|{type:'hello';role:'listen';history:RoomEvent[]})=>controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
      const sendFinal=(event:RoomEvent)=>{if(!seen.has(event.id)){seen.add(event.id);send(event);}};
      send({type:'hello',role:'listen',history});
      unsubscribe=subscribeRoom(roomId,sendFinal);
      poll=setInterval(()=>{void persistedHistory(roomId).then(events=>events.forEach(sendFinal));},1000);
    },
    cancel(){if(poll)clearInterval(poll);unsubscribe();},
  });
  return new Response(stream,{headers:{'Content-Type':'text/event-stream','Cache-Control':'no-cache, no-transform','X-Accel-Buffering':'no'}});
}
