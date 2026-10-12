import json
from types import SimpleNamespace
import pytest
from app.agent_quality import check_translation
from app import agent_suite


def test_chinese_adjacent_numbers_are_checked_without_claiming_accuracy():
    assert 'numeric_value_needs_review' in check_translation('請在10分鐘後返回。','Return in 20 minutes.','zh-en')
    assert check_translation('請在１０分鐘後返回。','Return in 10 minutes.','zh-en') == []
    assert 'numeric_value_needs_review' in check_translation('10分鐘','ten minutes','zh-en')
    assert check_translation('第5頁','5ページです。','zh-ja') == []


def test_language_and_locked_terminology_checks():
    assert 'invalid_target_language_or_format' in check_translation('不要離開。','不要離開。','zh-en')
    terms=[{'zh':'禪修','en':'Chan practice','lock':True}]
    assert 'locked_term_needs_review' in check_translation('我們開始禪修。','Start meditation.','zh-en',terms)
    assert check_translation('我們開始禪修。','Start Chan practice.','zh-en',terms) == []


def test_real_model_temporal_error_is_flagged():
    assert 'temporal_relation_needs_review' in check_translation('請在10分鐘後返回。','Return within 10 minutes.','zh-en')
    assert check_translation('請在10分鐘後返回。','Return after 10 minutes.','zh-en') == []
    assert 'temporal_relation_needs_review' in check_translation('10分鐘後返回。','10分以内に戻ってください。','zh-ja')


def test_direct_inference_blocks_embedding_until_it_finishes():
    from app.runtime_activity import work,snapshot
    from app.embed import IdleGate
    gate=IdleGate(probe=lambda:{'pending':0,**snapshot()},idle_s=0)
    with work('translate'):
        assert snapshot()['desktop_translate_busy']==1
        assert gate.check()[0] is False
    assert snapshot()['desktop_translate_busy']==0
    assert gate.check()[0] is True


def test_worker_has_fixed_program_no_shell_and_tracing_off(tmp_path,monkeypatch):
    monkeypatch.setattr(agent_suite,'suite_root',lambda:tmp_path)
    py=tmp_path/'speech'/('Scripts/python.exe' if agent_suite.os.name=='nt' else 'bin/python')
    py.parent.mkdir(parents=True);py.write_text('test executable path')
    observed={}
    def runner(command,**kwargs):
        observed.update(command=command,kwargs=kwargs)
        return SimpleNamespace(returncode=0,stdout=b'ZEN_AGENT_RESULT={"text":"Hello"}\n')
    assert agent_suite.call_worker('translate',{'input_text':'test'},runner)['text']=='Hello'
    assert observed['command'][-1]=='translate'
    assert 'shell' not in observed['kwargs']
    assert observed['kwargs']['env']['LANGSMITH_TRACING']=='false'
    assert observed['kwargs']['timeout']==61
    with pytest.raises(ValueError):
        agent_suite.call_worker('powershell',{},runner)


def test_worker_rejects_oversized_request_and_ambiguous_results(tmp_path,monkeypatch):
    monkeypatch.setattr(agent_suite,'suite_root',lambda:tmp_path)
    py=tmp_path/'speech'/('Scripts/python.exe' if agent_suite.os.name=='nt' else 'bin/python')
    py.parent.mkdir(parents=True);py.write_text('test path')
    def runner(*args,**kwargs):
        return SimpleNamespace(returncode=0,stdout=b'ZEN_AGENT_RESULT={}\nZEN_AGENT_RESULT={}\n')
    with pytest.raises(ValueError):
        agent_suite.call_worker('translate',{'input_text':'a'*256001},runner)
    with pytest.raises(RuntimeError):
        agent_suite.call_worker('translate',{},runner)


def test_heavy_model_is_not_started_with_insufficient_memory(tmp_path,monkeypatch):
    monkeypatch.setattr(agent_suite,'suite_root',lambda:tmp_path)
    monkeypatch.setattr(agent_suite,'available_memory_gb',lambda:.5)
    py=tmp_path/'evaluation'/('Scripts/python.exe' if agent_suite.os.name=='nt' else 'bin/python')
    py.parent.mkdir(parents=True);py.write_text('test path')
    def runner(*args,**kwargs):
        raise AssertionError('A heavy model must not start while memory is occupied')
    with pytest.raises(RuntimeError,match='記憶體'):
        agent_suite.call_worker('evaluate',{},runner)


def test_topic_context_handles_languages_and_corrupt_database(tmp_path,monkeypatch):
    import sqlite3
    from app import agent_context
    path=tmp_path/'context.sqlite3'
    monkeypatch.setattr(agent_context,'cache_path',lambda:path)
    assert agent_context.context_for_topic('課程','ja')==[]
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE topic_context(key TEXT PRIMARY KEY,references_json TEXT)')
        conn.execute('INSERT INTO topic_context VALUES(?,?)',
                     (agent_context.topic_key('課程'),json.dumps([{'zh':'呼吸','en':'breathing','ja':'呼吸'}])))
    assert agent_context.context_for_topic('課程','en')[0]['target']=='breathing'
    assert agent_context.context_for_topic('課程','ja')[0]['target']=='呼吸'
    assert agent_context.context_for_topic('另一堂課','en')==[]
    assert agent_context.context_for_topic('課程','invalid')==[]
    path.write_bytes(b'corrupt')
    assert agent_context.context_for_topic('課程','ja')==[]


def test_optional_speech_bootstrap_does_not_use_other_profiles(tmp_path,monkeypatch):
    import sys
    from app.speech_extras import bootstrap
    monkeypatch.setattr(agent_suite,'suite_root',lambda:tmp_path)
    monkeypatch.setenv('ZEN_AGENT_SUITE_SPEECH','1')
    assert bootstrap() is False
    folder=tmp_path/'speech';folder.mkdir()
    (folder/'installation.json').write_text(json.dumps({'profile':'evaluation'}))
    (folder/'pyvenv.cfg').write_text(f'version = {sys.version_info.major}.{sys.version_info.minor}.0\n')
    assert bootstrap() is False
