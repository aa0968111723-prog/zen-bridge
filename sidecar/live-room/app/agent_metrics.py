"""Bounded operational history. Never stores audio, utterances or credentials."""
import time
from collections import deque
from threading import Lock

class AgentMetrics:
    def __init__(self,limit=100):
        self.started=time.monotonic();self.events=deque(maxlen=limit);self.lock=Lock();self.completed=0;self.failed=0
    def record(self,kind,elapsed_ms,ok,audio_seconds=None,route=None):
        event={'kind':kind,'elapsed_ms':round(elapsed_ms),'ok':bool(ok),'at':time.time()}
        if audio_seconds is not None:
            event['audio_seconds']=audio_seconds
            if audio_seconds>0:event['rtf']=round(elapsed_ms/1000/audio_seconds,3)
        if route in ('local','cloud'):event['route']=route
        with self.lock:
            self.events.append(event);self.completed+=int(bool(ok));self.failed+=int(not ok)
    def snapshot(self):
        with self.lock:return {'uptime_s':round(time.monotonic()-self.started),'completed':self.completed,'failed':self.failed,'history':list(self.events)}
