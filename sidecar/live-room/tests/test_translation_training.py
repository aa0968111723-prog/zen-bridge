import json
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from pathlib import Path
import pytest
from app.translation_training import TrainingManager,confirmed_pairs,split_pairs,normalize,digest
from tests.test_automation import database,segment


def add_memory(database,source,target,**changes):
    import uuid
    values={'id':str(uuid.uuid4()),'zh':source,'en':target,'status':'verified','created_at':'2026-10-15','updated_at':'2026-10-15'}
    values.update(changes)
    with sqlite3.connect(database[0]) as db:
        db.execute('INSERT INTO memories('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))


def test_only_confirmed_non_fixture_translations_enter_training(database):
    add_memory(database,'請呼吸。','Breathe.')
    add_memory(database,'請呼吸。','Breathe.')
    add_memory(database,'未確認。','Unconfirmed.',status='candidate')
    add_memory(database,'已封存。','Archived.',status='archived')
    add_memory(database,'測試句。','Fixture.',context='[GROK-TEST] automation')
    add_memory(database,'不完整。','')
    rows=confirmed_pairs(database[0])
    assert len(rows)==1 and rows[0]['origin']=='human-verified-memory'
    assert rows[0]['source']=='請呼吸。' and rows[0]['direction']=='zh-en'


def test_splits_are_deterministic_and_source_groups_never_leak():
    rows=[{'source':f'公開原文 {i}','target':f'Public target {i}','direction':'zh-en'} for i in range(50)]
    rows.append({**rows[0],'target':'別の訳','direction':'zh-ja'})
    a,b=split_pairs(rows);c,d=split_pairs(list(reversed(rows)))
    assert {normalize(r['source']) for r in a}.isdisjoint({normalize(r['source']) for r in b})
    assert len(b)>=10 and len(a)>=40
    assert sorted((r['source'],r['target']) for r in a)==sorted((r['source'],r['target']) for r in c)
    with pytest.raises(ValueError):split_pairs(rows[:49])


def test_real_database_snapshot_is_immutable_and_reproducible(database):
    for i in range(50):add_memory(database,f'確認原文 {i}',f'Confirmed sentence {i}')
    manager=TrainingManager(database[0].parent,memory=lambda:12)
    folder=manager.prepare();assert manager.prepare()==folder
    manifest=json.loads((folder/'manifest.json').read_text())
    assert manifest['train_count']==40 and manifest['eval_count']==10
    assert all(digest(folder/name)==value for name,value in manifest['files'].items())
    add_memory(database,'新增確認原文。','New confirmed sentence.')
    assert manager.prepare()!=folder
    assert all(digest(folder/name)==value for name,value in manifest['files'].items())


def test_training_waits_for_reviewed_data_instead_of_learning_raw_captions(database):
    segment(database,en='A raw machine translation.')
    manager=TrainingManager(database[0].parent,activity=lambda:{'desktop_idle_s':10000},memory=lambda:12)
    assert manager.start_run()['status']=='waiting-for-human-verified-data'
    assert manager.process is None


def test_training_never_starts_during_capture_or_low_memory(database):
    for i in range(50):add_memory(database,f'確認原文 {i}',f'Confirmed sentence {i}')
    manager=TrainingManager(database[0].parent,activity=lambda:{'desktop_idle_s':10,'desktop_inflight':1},memory=lambda:12)
    assert manager.start_run()['status']=='waiting-for-offline-idle'
    manager.activity=lambda:{'desktop_idle_s':10000};manager.memory=lambda:3
    assert manager.start_run()['status']=='waiting-for-memory' and manager.process is None


def test_foreground_preempts_only_app_owned_training_process(database,tmp_path):
    from app.translation_training import atomic_json
    manager=TrainingManager(database[0].parent,activity=lambda:{'desktop_inflight':1},memory=lambda:12)
    run=tmp_path/'run-owned';run.mkdir();atomic_json(run/'state.json',{'id':run.name,'status':'running','step':2})
    process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    manager.process=process;manager.current=run
    try:
        manager.tick()
        assert process.poll() is not None and manager.process is None
        assert json.loads((run/'state.json').read_text())['status']=='paused-for-foreground'
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=3)


def test_hermes_bridge_cannot_run_arbitrary_commands_or_approve_data(monkeypatch):
    from scripts import zen_training_mcp as bridge
    names={tool['name'] for tool in bridge.TOOLS}
    assert 'approve' not in ' '.join(names) and len(names)==6
    for arguments in ({'command':'arbitrary shell'},{'url':'https://external.example'},{'path':'private.db'}):
        result=bridge.handle({'id':1,'method':'tools/call','params':{'name':'start_translation_finetune','arguments':arguments}})
        assert result['error']['code']==-32602
    assert bridge.handle({'method':'notifications/initialized'}) is None
    assert bridge.handle({'id':1,'method':'tools/list'})['result']['tools']==bridge.TOOLS


@pytest.mark.anyio
async def test_training_backend_requires_host_and_same_origin(database):
    from httpx import AsyncClient,ASGITransport
    from tests.test_core import make_app
    app=make_app();app.state.training=TrainingManager(database[0].parent)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1:8780') as client:
        for method,url in [('GET','/api/training'),('POST','/api/training/start'),('POST','/api/training/prepare')]:
            assert (await client.request(method,url)).status_code==401
        headers={'authorization':'Bearer '+app.state.token}
        response=await client.get('/api/training',headers=headers)
        assert response.json()['method']=='LoRA weight fine-tuning' and response.headers['cache-control']=='no-store'
        assert (await client.post('/api/training/start',headers=headers)).json()['status']=='waiting-for-human-verified-data'
        assert (await client.post('/api/training/prepare',headers=headers)).status_code==409
        assert (await client.post('/api/training/start',headers={**headers,'origin':'https://foreign.example'})).status_code==403


def test_generation_gate_rejects_regression_and_model_registry_checks_it(tmp_path):
    from app.training_evaluation import regression_gate,activate_model,active_model,character_f2
    rows=[{'source':'請在10分鐘後返回。','target':'Return after 10 minutes.','direction':'zh-en'} for _ in range(10)]
    baseline=['Return after 10 minutes.']*10
    wrong=['Return within 20 minutes.']*10
    gate=regression_gate(rows,baseline,wrong)
    assert not gate['passed']
    assert character_f2('exact translation','exact translation')==100
    folder=tmp_path/('run-'+'a'*24);folder.mkdir()
    with pytest.raises(ValueError):activate_model(folder,gate,root=tmp_path)
    gate=regression_gate(rows,wrong,baseline)
    assert gate['passed']
    activate_model(folder,gate,root=tmp_path)
    assert active_model(tmp_path)=='zen-translation:'+folder.name
    assert active_model(tmp_path,direction='zh-ja') is None
    (tmp_path/'training/active-model.json').write_text(json.dumps({'status':'published','model':'https://remote.example','generation_gate':{'passed':True}}))
    assert active_model(tmp_path) is None


def test_generation_gate_does_not_let_english_offset_japanese_regression(tmp_path):
    from app.training_evaluation import regression_gate,activate_model,active_model
    rows=[{'source':f'英文句 {i}','target':f'English sentence {i}.','direction':'zh-en'} for i in range(10)]
    rows+=[{'source':f'日文句 {i}','target':f'日本語の文 {i}。','direction':'zh-ja'} for i in range(10)]
    baseline=[row['target'] for row in rows]
    candidate=list(baseline)
    for i in range(10,20):candidate[i]='return within twenty minutes'
    gate=regression_gate(rows,['short']*20,candidate)
    assert gate['per_direction']['zh-en']['passed'] is True
    assert gate['per_direction']['zh-ja']['passed'] is False
    assert gate['directions']==['zh-en']
    assert 'zh-ja' in gate['rejected_directions']
    averaged=(sum(gate['per_direction'][name]['candidate_score'] for name in ('zh-en','zh-ja'))/2
              - sum(gate['per_direction'][name]['base_score'] for name in ('zh-en','zh-ja'))/2)
    assert averaged>1
    folder=tmp_path/('run-'+'c'*24);folder.mkdir()
    activate_model(folder,gate,root=tmp_path)
    assert active_model(tmp_path,direction='zh-en')=='zen-translation:'+folder.name
    assert active_model(tmp_path,direction='zh-ja') is None
    thin=rows[:10]+rows[10:15]
    thin_gate=regression_gate(thin,['x']*15,[row['target'] for row in thin])
    assert thin_gate['per_direction']['zh-ja']['passed'] is False
    assert 'zh-ja' not in thin_gate['directions']


def test_invalid_trained_model_falls_back_to_base_locally(monkeypatch):
    from app import training_evaluation
    from app.desktop_llm import DesktopTranslator
    monkeypatch.setenv('ZEN_BRIDGE_LOCAL','1')
    monkeypatch.setattr(training_evaluation,'active_model',lambda **kwargs:'zen-translation:run-'+'a'*24)
    client=DesktopTranslator();calls=[]
    def request(base,path,payload,**kwargs):
        calls.append((base,path,payload['model']))
        if len(calls)==1:raise RuntimeError('Candidate unavailable')
        return {'message':{'content':'Return to breathing.'},'done_reason':'stop'}
    client._request=request
    assert client.translate('Translate.','請回到呼吸。','cloud')['model']==client.local_model
    assert len(calls)==2 and all(path=='/api/chat' and base=='http://127.0.0.1:11434' for base,path,model in calls)


def test_japanese_import_requires_explicit_review_and_is_not_english_memory(database,tmp_path):
    from scripts.import_training_pairs import import_reviewed
    file=tmp_path/'reviewed.jsonl'
    item={'source':'請注意呼吸。','target':'呼吸に注意してください。','direction':'zh-ja','approved':False}
    file.write_text(json.dumps(item),encoding='utf-8')
    with pytest.raises(ValueError):import_reviewed(file,database[0].parent)
    item['approved']=True;file.write_text(json.dumps(item),encoding='utf-8')
    assert import_reviewed(file,database[0].parent)['distinct_pairs']==1
    rows=confirmed_pairs(database[0])
    assert len(rows)==1 and rows[0]['direction']=='zh-ja' and rows[0]['origin']=='human-reviewed-import'


def test_model_rollback_preserves_both_models_without_deleting_weights(tmp_path):
    from app.training_evaluation import activate_model,active_model,rollback_model
    gate={'passed':True,'heldout_pairs':10,'directions':['zh-en']}
    a=tmp_path/('run-'+'a'*24);b=tmp_path/('run-'+'b'*24);a.mkdir();b.mkdir()
    activate_model(a,gate,root=tmp_path);activate_model(b,gate,root=tmp_path)
    assert rollback_model(tmp_path)['status']=='restored-previous'
    assert active_model(tmp_path)=='zen-translation:'+a.name and a.exists() and b.exists()


def test_worker_run_lock_is_exclusive_and_released_after_exit(tmp_path):
    from scripts.train_translation_lora import run_lock,RunAlreadyOwned
    with run_lock(tmp_path):
        with pytest.raises(RunAlreadyOwned):
            with run_lock(tmp_path):pytest.fail('Duplicate trainer acquired the same run')
    with run_lock(tmp_path):pass


def test_existing_pr55_export_is_consumed_without_reassigning_its_holdout(database,tmp_path):
    from scripts.import_training_pairs import import_pr55_export
    folder=tmp_path/'mt';folder.mkdir()
    for split,values in [('train',range(40)),('test',range(40,50))]:
        rows=[{'messages':[{'role':'system','content':'Translate.'},{'role':'user','content':f'公開原文 {i}'},
                           {'role':'assistant','content':f'Public translation {i}'}]} for i in values]
        (folder/('zh-en.'+split+'.jsonl')).write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows),encoding='utf-8')
    assert import_pr55_export(folder,database[0].parent)['original_splits_preserved']
    train,heldout=split_pairs(confirmed_pairs(database[0]))
    assert len(train)==40 and len(heldout)==10
    assert all(row['export_split']=='test' for row in heldout)
    add_memory(database,'公開原文 40','Public translation 40')
    train,heldout=split_pairs(confirmed_pairs(database[0]))
    assert all(row['source']!='公開原文 40' for row in train)
