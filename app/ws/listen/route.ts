import {roomHistory,subscribeRoom,type RoomEvent} from '@/lib/rooms';

export const dynamic = 'force-dynamic';

export function GET(request: Request) {
  const roomId = new URL(request.url).searchParams.get('room')?.slice(0, 80) || '';
  if (!roomId) return Response.json({error:'缺少 room。'},{status:400});
  const pairConstructor = (globalThis as unknown as {WebSocketPair?:new()=>{0:WebSocket;1:WebSocket}}).WebSocketPair;
  if (request.headers.get('upgrade')?.toLowerCase()==='websocket'&&pairConstructor) {
    const pair=new pairConstructor(),client=pair[0],server=pair[1] as WebSocket&{accept():void};
    server.accept();
    server.send(JSON.stringify({type:'hello',role:'listen',history:roomHistory(roomId)}));
    const unsubscribe=subscribeRoom(roomId,event=>server.send(JSON.stringify(event)));
    server.addEventListener('close',unsubscribe,{once:true});
    return new Response(null,{status:101,webSocket:client} as ResponseInit&{webSocket:WebSocket});
  }
  const encoder=new TextEncoder();
  let unsubscribe=()=>{};
  const stream=new ReadableStream({
    start(controller){
      const send=(event:RoomEvent|{type:'hello';role:'listen';history:RoomEvent[]})=>controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
      send({type:'hello',role:'listen',history:roomHistory(roomId)});
      unsubscribe=subscribeRoom(roomId,send);
    },
    cancel(){unsubscribe();},
  });
  return new Response(stream,{headers:{'Content-Type':'text/event-stream','Cache-Control':'no-cache, no-transform','X-Accel-Buffering':'no'}});
}
