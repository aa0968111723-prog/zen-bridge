"""Pre-class semantic context cache; live translation never starts embeddings."""
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path


def cache_path():
    return Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'ZenBridge/agent-suite/context-cache.sqlite3'


def topic_key(topic):
    return hashlib.sha256(topic.strip().encode()).hexdigest()


def cosine(a,b):
    if len(a)!=len(b) or not a or any(not math.isfinite(x) for x in [*a,*b]):
        raise ValueError('Invalid context vectors')
    norm=math.sqrt(sum(x*x for x in a)*sum(x*x for x in b))
    return sum(x*y for x,y in zip(a,b))/norm if norm else 0.0


def context_for_topic(topic,lang):
    if not isinstance(topic,str) or lang not in ('en','ja') or not 1<=len(topic)<=1000:
        return []
    path=cache_path()
    if not path.is_file():
        return []
    try:
        conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)
        try:
            row=conn.execute('SELECT references_json FROM topic_context WHERE key=?',(topic_key(topic),)).fetchone()
        finally:
            conn.close()
        if row is None or len(row[0])>20000:return []
        rows=json.loads(row[0])
        if not isinstance(rows,list):return []
        return [{'zh':r['zh'],'target':r[lang],'source':'topic-reference'} for r in rows[:3]
                if isinstance(r,dict) and all(isinstance(r.get(k),str) and len(r[k])<=240 for k in ('zh',lang))]
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
        return []
