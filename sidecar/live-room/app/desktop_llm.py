"""Desktop translation routes: local Ollama and loopback Hermes, no tool execution."""
import json
import os
import time
import urllib.request
from urllib.parse import urlsplit

def loopback_url(value):
    url=urlsplit(value)
    if url.scheme!='http' or url.hostname not in ('localhost','127.0.0.1','::1') or url.username or url.password or url.query or url.fragment:
        raise ValueError('Desktop model endpoints must be loopback HTTP')
    return value.rstrip('/')

class DesktopTranslator:
    def __init__(self):
        self.local=loopback_url(os.getenv('ZEN_LOCAL_LLM_URL','http://127.0.0.1:11434'))
        self.hermes=loopback_url(os.getenv('ZEN_HERMES_URL','http://127.0.0.1:8645'))
        self.local_model=os.getenv('ZEN_LOCAL_LLM_MODEL','qwen3:1.7b')
        self.cloud_model=os.getenv('ZEN_HERMES_MODEL','grok-4.6')
        self.key=os.getenv('ZEN_HERMES_API_KEY','')
        self.last={};self.calls=0
        self._health_at=0.;self._hermes_health={'reachable':False,'authenticated':False}
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self,base,path,payload,authenticated=False):
        headers={'Content-Type':'application/json'}
        if authenticated:
            headers['Authorization']='Bearer '+(self.key or 'local-proxy')
        request=urllib.request.Request(base+path,data=json.dumps(payload).encode(),headers=headers,method='POST')
        with self.opener.open(request,timeout=45) as response:
            data=response.read(200_001)
        if len(data)>200_000:raise ValueError('Model response exceeds limit')
        return json.loads(data)

    def translate(self,instructions,input_text,mode):
        if mode not in ('local','cloud','hybrid'):raise ValueError('Unknown translation mode')
        if not isinstance(instructions,str) or not isinstance(input_text,str) or len(instructions)>6000 or not 1<=len(input_text)<=24000:
            raise ValueError('Invalid translation input')
        messages=[{'role':'system','content':instructions},{'role':'user','content':input_text}]
        started=time.monotonic();routes=['local','cloud'] if mode=='hybrid' else [mode]
        for route in routes:
            try:
                if route=='local':
                    response=self._request(self.local,'/api/chat',{'model':self.local_model,'messages':messages,'stream':False,'think':False,'keep_alive':'30s','options':{'temperature':0.1,'num_ctx':2048,'num_predict':180,'num_thread':2}})
                    text=response.get('message',{}).get('content','');model=self.local_model
                    if response.get('done_reason')=='length':raise ValueError('Incomplete translation')
                else:
                    response=self._request(self.hermes,'/v1/chat/completions',{'model':self.cloud_model,'messages':messages,'stream':False,'tool_choice':'none'})
                    choice=response.get('choices',[{}])[0];text=choice.get('message',{}).get('content','');model=self.cloud_model
                    if choice.get('finish_reason') not in (None,'stop'):raise ValueError('Incomplete translation')
                if not isinstance(text,str) or not text.strip() or len(text)>16000:raise ValueError('Invalid model text')
                if text.strip()==instructions.strip():raise ValueError('Model echoed instructions')
                self.calls+=1;self.last={'mode':mode,'route':route,'model':model,'elapsed_ms':round((time.monotonic()-started)*1000),'fallback':route!=routes[0]}
                return {'text':text.strip(),**self.last}
            except Exception:
                if route==routes[-1]:raise RuntimeError('Selected desktop translation route is unavailable') from None

    def status(self):
        # A dummy proxy key says nothing about the upstream OAuth account.
        # Probe only loopback health; never use /models or inference for status.
        now=time.monotonic()
        if now-self._health_at>=15:
            health={'reachable':False,'authenticated':False}
            try:
                request=urllib.request.Request(self.hermes+'/health')
                with self.opener.open(request,timeout=1) as response:
                    raw=response.read(4097)
                if len(raw)<=4096:
                    body=json.loads(raw)
                    health={'reachable':True,'authenticated':body.get('authenticated') is True}
            except Exception:
                pass
            self._hermes_health=health;self._health_at=now
        return {'local_model':self.local_model,'cloud_model':self.cloud_model,
                'hermes_auth_configured':self._hermes_health['authenticated'],
                'hermes_reachable':self._hermes_health['reachable'],
                'calls':self.calls,'last':self.last}
