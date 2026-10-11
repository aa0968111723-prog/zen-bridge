"""Typed optional workers. No shell tools or model-selected file operations."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['LANGSMITH_TRACING']='false'
os.environ['LANGCHAIN_TRACING_V2']='false'
os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
os.environ['PYANNOTE_METRICS_ENABLED']='false'
_dll_handles=[]
if os.name=='nt' and hasattr(os,'add_dll_directory'):
    vendor=ROOT/'app/vendor/msvc'
    if vendor.is_dir():
        _dll_handles.append(os.add_dll_directory(str(vendor)))


def translate_graph(request):
    from typing import TypedDict
    from langgraph.graph import StateGraph,START,END
    from app.desktop_llm import DesktopTranslator
    from app.agent_quality import check_translation
    class State(TypedDict,total=False):
        instructions:str
        input_text:str
        mode:str
        attempts:int
        result:dict
        warnings:list
    payload=json.loads(request['input_text'])
    if not isinstance(payload,dict):
        raise ValueError('Structured translation input required')
    source=payload.get('currentUtterance',payload.get('text',''))
    if not isinstance(source,str) or not 1<=len(source)<=8000:
        raise ValueError('Invalid current utterance')
    direction=payload.get('direction','zh-en')
    if direction not in ('zh-en','zh-ja','en-zh'):
        raise ValueError('Invalid translation direction')
    glossary=payload.get('glossary',[])
    if not isinstance(glossary,list) or len(glossary)>200 or any(not isinstance(term,dict) for term in glossary):
        raise ValueError('Invalid terminology context')
    client=DesktopTranslator()
    started=time.monotonic()
    def translate(state):
        instruction=state['instructions']
        if state.get('attempts',0):
            instruction+='\nReview the original utterance and preserve its numbers, required terminology, negation, and temporal relations (after versus within). Return only the corrected translation.'
        return {'result':client.translate(instruction,state['input_text'],state['mode']),
                'attempts':state.get('attempts',0)+1}
    def check(state):
        return {'warnings':check_translation(source,state['result']['text'],direction,glossary)}
    def retrieve(state):
        from app.agent_context import context_for_topic
        data=json.loads(state['input_text'])
        activity=data.get('activity')
        hints=context_for_topic(activity.get('topic','') if isinstance(activity,dict) else '',
                                'ja' if direction=='zh-ja' else 'en') if direction!='en-zh' else []
        if not hints:
            return {}
        existing=data.get('dictionaryReferences',[])
        if not isinstance(existing,list):existing=[]
        data['dictionaryReferences']=(existing+hints)[:3]
        return {'input_text':json.dumps(data,ensure_ascii=False)}
    graph=StateGraph(State)
    graph.add_node('retrieve',retrieve)
    graph.add_node('translate',translate)
    graph.add_node('check',check)
    graph.add_edge(START,'retrieve');graph.add_edge('retrieve','translate'); graph.add_edge('translate','check')
    graph.add_conditional_edges('check',lambda s:'translate' if s['warnings'] and s['attempts']<2
                               and time.monotonic()-started<10 and len(s['instructions'])<5800 else END)
    result=graph.compile().invoke({**request,'attempts':0},{'recursion_limit':8})
    return {**result['result'],'elapsed_ms':round((time.monotonic()-started)*1000),
            'quality':{'warnings':result['warnings'],'attempts':result['attempts'],
            'needs_review':bool(result['warnings'])},'agent':'langgraph'}


def enhance(request):
    import wave
    import numpy as np
    import sherpa_onnx
    with wave.open(request['input_wav'],'rb') as audio:
        if audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getnframes()/audio.getframerate()>120:
            raise ValueError('Expected mono PCM16 audio of at most 120 seconds')
        rate=audio.getframerate()
        samples=np.frombuffer(audio.readframes(audio.getnframes()),dtype='<i2').astype(np.float32)/32768
    config=sherpa_onnx.OfflineSpeechDenoiserConfig()
    config.model.gtcrn.model=request['model']
    config.model.num_threads=1
    cleaned=sherpa_onnx.OfflineSpeechDenoiser(config).run(samples,rate)
    output=Path(request['output_wav'])
    if output.exists() or output.resolve()==Path(request['input_wav']).resolve():
        raise ValueError('Refusing to overwrite audio')
    with output.open('xb') as destination, wave.open(destination,'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(cleaned.sample_rate)
        audio.writeframes((np.clip(np.asarray(cleaned.samples),-1,1)*32767).astype('<i2').tobytes())
    return {'samples':len(cleaned.samples),'sample_rate':cleaned.sample_rate,'model':'gtcrn'}


def diarize(request):
    import torch
    import soundfile as sf
    from pyannote.audio import Pipeline
    model=Path(request['model_dir'])
    if not model.is_dir():
        raise ValueError('Speaker model not downloaded; complete HF conditions first')
    info=sf.info(request['input_wav'])
    if info.duration>3600 or info.channels>2 or info.frames>172800000:
        raise ValueError('Speaker audio exceeds the one-hour job limit')
    samples,rate=sf.read(request['input_wav'],dtype='float32',always_2d=True)
    torch.set_num_threads(2)
    pipeline=Pipeline.from_pretrained(str(model))
    result=pipeline({'waveform':torch.from_numpy(samples.mean(axis=1)[None,:]),'sample_rate':rate})
    return {'turns':[{'start':turn.start,'end':turn.end,'speaker':speaker}
                    for turn,speaker in result.speaker_diarization]}


def evaluate(request):
    import torch
    from comet import load_from_checkpoint
    checkpoint=Path(request['checkpoint'])
    if not checkpoint.is_file():
        raise ValueError('COMET checkpoint not downloaded')
    rows=request['rows']
    if not isinstance(rows,list) or not 1<=len(rows)<=64 or any(not isinstance(r,dict) or
            any(not isinstance(r.get(k),str) or len(r[k])>8000 for k in ('src','mt')) for r in rows):
        raise ValueError('Invalid evaluation batch')
    torch.set_num_threads(2)
    model=load_from_checkpoint(str(checkpoint))
    result=model.predict(rows,batch_size=4,gpus=0,num_workers=0)
    return {'scores':list(result.scores),'system_score':float(result.system_score)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=['translate','enhance','diarize','evaluate'])
    args=parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    raw=sys.stdin.buffer.read(256001)
    if len(raw)>256000:
        raise ValueError('Worker request too large')
    request=json.loads(raw)
    handler={'translate':translate_graph,'enhance':enhance,'diarize':diarize,'evaluate':evaluate}[args.operation]
    result=handler(request)
    print('ZEN_AGENT_RESULT='+json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
