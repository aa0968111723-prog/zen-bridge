"""Build a course-topic cache only after live inference is safely idle."""
import argparse
import json
from pathlib import Path
import sys
import sqlite3
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.admin.live_client import LiveRoomClient,LiveRefused
from app.embed import IdleGate,OllamaEmbedder
from app.reference_pack import entries
from app.agent_context import cosine,cache_path,topic_key


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--topic',required=True)
    args=parser.parse_args()
    if not 1<=len(args.topic)<=1000:
        parser.error('Topic must contain 1–1000 characters')
    client=LiveRoomClient('http://127.0.0.1:8780')
    def probe():
        status,data=client.call('GET','/api/metrics')
        if status!=200 or not {'desktop_inflight','desktop_translate_busy','draft_sessions'}<=data.keys():
            raise RuntimeError('Update the live App before enabling context indexing')
        return data
    gate=IdleGate(probe=probe,idle_s=10)
    embedder=OllamaEmbedder(num_thread=1,keep_alive='2m')
    rows=list(entries())
    vectors=[]
    until=time.monotonic()+25
    while not gate.check()[0]:
        if time.monotonic()>=until:
            raise RuntimeError('Context indexing deferred while the App is busy')
        time.sleep(1)
    for i in range(0,len(rows),16):
        ok,why=gate.confirm()
        if not ok:
            raise RuntimeError('Context indexing deferred: '+why)
        vectors.extend(embedder.embed([r['zh'] for r in rows[i:i+16]]))
    if not gate.confirm()[0]:
        raise RuntimeError('Context query deferred while live inference is busy')
    query=embedder.embed([args.topic])[0]
    ranked=sorted(zip(rows,vectors),key=lambda item:cosine(query,item[1]),reverse=True)
    path=cache_path();path.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(path)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS topic_context(key TEXT PRIMARY KEY,model TEXT NOT NULL,references_json TEXT NOT NULL)')
        conn.execute('INSERT INTO topic_context VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET model=excluded.model,references_json=excluded.references_json',
                     (topic_key(args.topic),embedder.model,json.dumps([r for r,_ in ranked[:8]],ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    print(json.dumps({'indexed':len(rows),'cached_references':8,'model':embedder.model}))


if __name__=='__main__':
    main()
