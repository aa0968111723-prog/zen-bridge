"""Paired desktop transport with independent ASR and translation lanes."""
import asyncio
import json
import os
import ssl
import certifi
from pathlib import Path
from urllib.parse import urlsplit
from websockets.asyncio.client import connect
from app.agent_requests import AgentRequests

async def run_agent(asr):
    url=os.getenv('ZEN_BRIDGE_AGENT_URL','');token=os.getenv('ZEN_BRIDGE_AGENT_TOKEN','')
    if not url or not token:return
    parsed=urlsplit(url)
    if parsed.scheme!='wss' or parsed.username or parsed.password or parsed.query or parsed.fragment or len(token)<32 or '\n' in token or '\r' in token:
        raise ValueError('Invalid Zen Bridge pairing configuration')
    root=Path(__file__).resolve().parents[1];(root/'tmp').mkdir(exist_ok=True)
    requests=AgentRequests(asr,root);tls=ssl.create_default_context(cafile=certifi.where())
    while True:
        try:
            if not asr.health():await asyncio.sleep(2);continue
            async with connect(url,additional_headers={'Authorization':'Bearer '+token},proxy=None,ssl=tls,max_size=12_000_000,ping_interval=20,ping_timeout=30,open_timeout=20) as socket:
                await socket.send(json.dumps({'type':'ready','protocol':1,'engine':'breeze-native','capabilities':['asr','translate','metrics']}))
                jobs=set();send_lock=asyncio.Lock()
                async def heartbeat():
                    while True:
                        telemetry=await asyncio.to_thread(requests.status)
                        async with send_lock:
                            await socket.send(json.dumps({'type':'telemetry','metrics':telemetry},ensure_ascii=True))
                        await asyncio.sleep(15)
                heartbeat_task=asyncio.create_task(heartbeat())
                async def handle(request,connection):
                    response=await requests.handle(request)
                    if response:
                        async with send_lock:
                            await connection.send(json.dumps(response,ensure_ascii=True))
                        telemetry=await asyncio.to_thread(requests.status)
                        async with send_lock:
                            await connection.send(json.dumps({'type':'telemetry','metrics':telemetry},ensure_ascii=True))
                try:
                    async for raw in socket:
                        request=json.loads(raw)
                        if request.get('type') not in ('transcribe','translate','status') or not isinstance(request.get('id'),str):continue
                        if len(jobs)>=4:
                            async with send_lock:await socket.send(json.dumps({'type':'control_result' if request['type']!='transcribe' else 'result','id':request['id'],'ok':False,'error':'Desktop queue full'}))
                            continue
                        def completed(job):
                            jobs.discard(job)
                            if not job.cancelled():job.exception()
                        job=asyncio.create_task(handle(request,socket));jobs.add(job);job.add_done_callback(completed)
                finally:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task,return_exceptions=True)
                    for job in jobs:job.cancel()
                    await asyncio.gather(*jobs,return_exceptions=True)
        except asyncio.CancelledError:raise
        except Exception:
            # Do not log speech, credentials, raw endpoint URLs or TLS errors.
            await asyncio.sleep(3)
