"""Offline generation regression gates and a validated local-model registry."""
from collections import Counter
import json
import math
import re
from app.translation_training import data_root,atomic_json


def character_f2(actual,expected):
    actual=''.join(actual.casefold().split());expected=''.join(expected.casefold().split())
    values=[]
    for n in range(1,7):
        a=Counter(actual[i:i+n] for i in range(max(0,len(actual)-n+1)))
        b=Counter(expected[i:i+n] for i in range(max(0,len(expected)-n+1)))
        if not b:continue
        overlap=sum((a&b).values());precision=overlap/max(1,sum(a.values()));recall=overlap/sum(b.values())
        values.append(5*precision*recall/(4*precision+recall) if precision+recall else 0)
    return 100*sum(values)/len(values) if values else 0


def regression_gate(rows,baseline,candidate):
    """Score each direction alone. A gain in one direction cannot offset a loss in another."""
    from app.agent_quality import check_translation
    if len(rows)<10 or len(rows)!=len(baseline) or len(rows)!=len(candidate):raise ValueError('Independent generation evaluation requires at least 10 aligned pairs')
    if any(not isinstance(x,str) or not x.strip() or len(x)>16000 for x in [*baseline,*candidate]):raise ValueError('Incomplete generation evaluation')
    grouped={}
    for index,row in enumerate(rows):
        grouped.setdefault(row['direction'],[]).append(index)
    per={};enabled=[];rejected=[]
    for direction,indexes in sorted(grouped.items()):
        base=sum(character_f2(baseline[i],rows[i]['target']) for i in indexes)/len(indexes)
        cand=sum(character_f2(candidate[i],rows[i]['target']) for i in indexes)/len(indexes)
        base_warnings=sum(len(check_translation(rows[i]['source'],baseline[i],direction)) for i in indexes)
        cand_warnings=sum(len(check_translation(rows[i]['source'],candidate[i],direction)) for i in indexes)
        eligible=len(indexes)>=10
        ok=eligible and cand>=base+1 and cand_warnings<=base_warnings
        per[direction]={'passed':ok,'eligible':eligible,'heldout_pairs':len(indexes),'base_score':base,'candidate_score':cand,
                        'base_warnings':base_warnings,'candidate_warnings':cand_warnings,
                        'reason':None if ok else ('insufficient-independent-samples' if not eligible else 'direction-regression')}
        (enabled if ok else rejected).append(direction)
    scores={name:sum(character_f2(text,row['target']) for row,text in zip(rows,texts))/len(rows)
            for name,texts in [('base',baseline),('candidate',candidate)]}
    warnings={name:sum(len(check_translation(row['source'],text,row['direction'])) for row,text in zip(rows,texts))
              for name,texts in [('base',baseline),('candidate',candidate)]}
    return {'passed':bool(enabled),'heldout_pairs':sum(per[name]['heldout_pairs'] for name in enabled),
            'directions':enabled,'rejected_directions':rejected,'per_direction':per,
            'metric':'character F2 (1-6 grams) per direction','base_score':scores['base'],'candidate_score':scores['candidate'],
            'base_warnings':warnings['base'],'candidate_warnings':warnings['candidate'],'minimum_improvement':1,
            'minimum_pairs_per_direction':10,'aggregate_does_not_decide':True,'general_quality_guaranteed':False}


def active_model(root=None,*,direction=None):
    try:
        record=json.loads(((root or data_root())/'training/active-model.json').read_text(encoding='utf-8'))
        if not isinstance(record,dict):return None
        name=record.get('model','')
        gate=record.get('generation_gate',{})
        if not isinstance(gate,dict):return None
        directions=gate.get('directions',[])
        per=gate.get('per_direction') or {}
        if direction is not None and direction not in directions:return None
        if direction is not None and isinstance(per,dict) and direction in per and per[direction].get('passed') is not True:return None
        if record.get('status')=='published' and gate.get('passed') is True and directions and re.fullmatch(r'zen-translation:run-[a-f0-9]{24}',name):return name
    except (OSError,ValueError,TypeError):pass
    return None


def activate_model(folder,gate,root=None):
    if not gate.get('passed') or gate.get('heldout_pairs',0)<10:raise ValueError('Model did not pass independent generation evaluation')
    if not re.fullmatch(r'run-[a-f0-9]{24}',folder.name):raise ValueError('Invalid model run')
    base=root or data_root();registry=base/'training';registry.mkdir(parents=True,exist_ok=True)
    path=registry/'active-model.json'
    if path.exists():
        previous=json.loads(path.read_text(encoding='utf-8'))
        atomic_json(registry/'previous-model.json',previous)
    atomic_json(path,{'status':'published','model':'zen-translation:'+folder.name,'run':folder.name,'generation_gate':gate})


def rollback_model(root=None):
    registry=(root or data_root())/'training';registry.mkdir(parents=True,exist_ok=True)
    active=registry/'active-model.json';previous=registry/'previous-model.json'
    current=json.loads(active.read_text(encoding='utf-8')) if active.exists() else {'status':'base'}
    restored=json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else {'status':'base'}
    atomic_json(previous,current);atomic_json(active,restored)
    return {'status':'restored-previous' if restored.get('status')=='published' else 'using-original-base-model'}
