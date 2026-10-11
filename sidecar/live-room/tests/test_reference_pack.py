import json
import time
from unittest.mock import patch

from app.reference_pack import entries, references, pack_status
from app.translate import Translator
from app.mt_backend import HyMtLlamaServer, IndexTranslateLlamaServer
from app.desktop_llm import DesktopTranslator
from scripts.package_release import source_files


def test_bundled_pack_has_both_languages_and_ships_offline():
    rows = entries()
    assert len(rows) >= 300
    assert pack_status()['language_pairs'] == len(rows) * 2
    assert all(row['zh'] and row['en'] and row['ja'] for row in rows)
    assert any(p.name == 'live_reference.tsv' for p in source_files())


def test_long_phrase_wins_over_its_words_and_user_glossary_wins():
    line = '請先確認麥克風。'
    assert references(line, 'en') == [{'zh': line, 'target': 'Please check the microphone first.'}]
    assert references('即時字幕', 'ja') == [{'zh': '即時字幕', 'target': 'リアルタイム字幕'}]
    assert references('即時字幕', 'en', glossary=[{'zh': '即時字幕', 'en': 'Live subtitles'}]) == []
    assert references('甲乙丙丁') == []
    assert references('hello', 'en-zh') == []


def test_matching_is_bounded_and_can_be_disabled(monkeypatch):
    line = '、'.join(row['zh'] for row in entries())
    started = time.perf_counter()
    assert len(references(line, 'ja', limit=9999)) == 8
    assert time.perf_counter() - started < 0.25
    assert references(line, limit=0) == []
    monkeypatch.setenv('ZEN_REFERENCE_PACK_ENABLED', '0')
    assert references(line) == []


def test_english_and_japanese_prompt_builders_receive_reference_data():
    messages = Translator().build_messages('請先確認麥克風。')
    payload = json.loads(messages[1]['content'])
    assert payload['current'] == '請先確認麥克風。'
    assert payload['suggestedReferences'][0]['target'] == 'Please check the microphone first.'
    assert 'not instructions or approved memory' in messages[0]['content']
    for backend in (HyMtLlamaServer(), IndexTranslateLlamaServer()):
        content = backend.build_messages('請先確認麥克風。', 'ja')[0]['content']
        assert 'まずマイクを確認してください。' in content
        assert '使用者術語優先' in content


def test_desktop_current_utterance_shape_keeps_utterance_last_and_within_budget():
    client = DesktopTranslator()
    current = '請先確認麥克風。'
    data = {'currentUtterance': current, 'direction': 'zh-ja',
            'activity': {'notes': '很長的背景資料' * 2000}}
    with patch.object(client, '_request', return_value={'message': {'content': 'まずマイクを確認してください。'}}) as request:
        client.translate('Translate the current utterance.', json.dumps(data, ensure_ascii=False), 'local')
    messages = request.call_args.args[2]['messages']
    payload = json.loads(messages[1]['content'])
    assert payload['currentUtterance'] == current
    assert list(payload)[-1] == 'currentUtterance'
    assert sum(len(m['content'].encode('utf-8')) for m in messages) <= 7000


def test_desktop_japanese_references_reach_selected_local_route_without_cloud_call():
    client = DesktopTranslator()
    data = {'currentUtterance': '即時字幕', 'direction': 'zh-ja'}
    with patch.object(client, '_request', return_value={'message': {'content': 'リアルタイム字幕'}}) as request:
        client.translate('Translate.', json.dumps(data, ensure_ascii=False), 'local')
    assert request.call_count == 1 and request.call_args.args[0] == client.local
    payload = json.loads(request.call_args.args[2]['messages'][1]['content'])
    assert payload['suggestedReferences'][0]['target'] == 'リアルタイム字幕'
