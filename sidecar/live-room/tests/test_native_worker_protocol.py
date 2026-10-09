import subprocess
import sys
from pathlib import Path
import pytest
from app.native_asr import NativeResidentAsr
from app.native_worker import sampling_strategy

@pytest.mark.parametrize('beam,expected', [(0, 0), (1, 0), (5, 1)])
def test_beam_search_requires_explicit_beam_size(beam, expected):
    assert sampling_strategy(beam) == expected

def engine(tmp_path, monkeypatch, behavior):
    helper = tmp_path / 'worker.py'
    helper.write_text("import json,sys,time\nprint('BREEZE_RESULT '+json.dumps({'ready':True}),flush=True)\nfor line in sys.stdin:\n request=json.loads(line)\n " + behavior + '\n', encoding='utf-8')
    real_popen = subprocess.Popen
    def launch(command, **kwargs):
        return real_popen([sys.executable, '-u', str(helper)], **kwargs)
    monkeypatch.setattr('app.native_asr.subprocess.Popen', launch)
    return NativeResidentAsr(tmp_path / 'model.bin', startup_timeout_s=3, inference_timeout_s=0.2)

def test_persistent_worker_reuses_process_and_closes(tmp_path, monkeypatch):
    asr = engine(tmp_path, monkeypatch, "print('BREEZE_RESULT '+json.dumps({'ok':True,'text':'中文測試'}),flush=True)")
    try:
        assert asr.start().ok
        process = asr.proc
        assert asr.start().ok and asr.proc is process and asr.loads == 1
        assert asr.transcribe(tmp_path / '語音.wav', '繁體中文').text == '中文測試'
        assert asr.transcribe(tmp_path / '語音.wav', '繁體中文').ok and asr.loads == 1
    finally:
        asr.close()
    assert process.poll() is not None and not asr.health()

def test_inference_timeout_terminates_worker(tmp_path, monkeypatch):
    asr = engine(tmp_path, monkeypatch, 'time.sleep(20)')
    assert asr.start().ok
    process = asr.proc
    result = asr.transcribe(tmp_path / 'audio.wav', '')
    assert not result.ok and '逾時' in result.error
    assert process.poll() is not None and not asr.health()

def test_worker_exit_does_not_report_success(tmp_path, monkeypatch):
    asr = engine(tmp_path, monkeypatch, 'sys.exit(1)')
    try:
        assert asr.start().ok
        result = asr.transcribe(tmp_path / 'audio.wav', '')
        assert not result.ok and '結束' in result.error
    finally:
        asr.close()
