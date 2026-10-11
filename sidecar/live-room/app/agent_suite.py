"""Isolated optional workers: fixed operations, bounded IPC, no shell execution."""
import json
import os
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
OPERATIONS={'translate':('speech',61),'enhance':('speech',120),
            'diarize':('diarization',900),'evaluate':('evaluation',900)}


def suite_root():
    return Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'ZenBridge'/'agent-suite'


def suite_status():
    result={}
    for profile in ('speech','evaluation','diarization'):
        folder=suite_root()/profile
        marker=folder/'installation.json'
        py=folder/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        result[profile]={'installed':py.is_file() and marker.is_file()}
        if profile in ('diarization','evaluation'):
            model=Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'ZenBridge/models'/(
                'diarization/community1' if profile=='diarization' else 'evaluation/cometkiwi')
            required=model/('config.yaml' if profile=='diarization' else 'checkpoints/model.ckpt')
            result[profile]['model_ready']=(model/'zen-model-origin.json').is_file() and required.is_file()
    return result


def call_worker(operation,payload,runner=subprocess.run):
    if operation not in OPERATIONS:
        raise ValueError('Unsupported agent operation')
    profile,timeout=OPERATIONS[operation]
    py=suite_root()/profile/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not py.is_file():
        raise RuntimeError('Optional agent profile is not installed')
    raw=json.dumps(payload,ensure_ascii=False).encode('utf-8')
    if len(raw)>256000:
        raise ValueError('Agent request exceeds limit')
    env=dict(os.environ,LANGSMITH_TRACING='false',LANGCHAIN_TRACING_V2='false',
             HF_HUB_DISABLE_TELEMETRY='1',PYANNOTE_METRICS_ENABLED='false',OMP_NUM_THREADS='2',MKL_NUM_THREADS='2')
    completed=runner([str(py),str(ROOT/'scripts/agent_suite_worker.py'),operation],
                     input=raw,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,
                     env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if completed.returncode:
        raise RuntimeError('Optional agent worker failed; check its model/profile readiness')
    if len(completed.stdout)>1000000:
        raise RuntimeError('Agent result exceeds limit')
    lines=[line for line in completed.stdout.decode('utf-8').splitlines() if line.startswith('ZEN_AGENT_RESULT=')]
    if len(lines)!=1:
        raise RuntimeError('Invalid worker result')
    return json.loads(lines[0].split('=',1)[1])


class AgentSuiteTranslator:
    def __init__(self,base):
        self.base=base
        self.last={}

    def translate(self,instructions,input_text,mode):
        result=call_worker('translate',{'instructions':instructions,'input_text':input_text,'mode':mode})
        if not isinstance(result,dict) or not isinstance(result.get('text'),str) or not result['text'].strip() or len(result['text'])>16000:
            raise RuntimeError('Invalid agent translation')
        self.last={k:result[k] for k in ('agent','route','elapsed_ms','quality') if k in result}
        return result

    def status(self):
        return {**self.base.status(),'agent_suite':{'profiles':suite_status(),'enabled':True,'last':self.last}}
