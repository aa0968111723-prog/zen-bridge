"""Install isolated optional AI tools without changing the live App or Bot Python."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    'speech': ['sherpa-onnx==1.13.8', 'opencc-python-reimplemented==0.1.7',
               'langgraph==1.2.14', 'numpy==2.3.5', 'onnxruntime==1.31.0', 'requests==2.34.2'],
    'evaluation': ['unbabel-comet==2.2.7'],
    'diarization': ['pyannote.audio==4.0.7', 'torchcodec==0.7.0', 'soundfile==0.14.0', 'huggingface_hub==2.2.0'],
}
CPU_TORCH = ['torch==2.8.0+cpu', 'torchaudio==2.8.0+cpu']


def suite_root():
    return Path(os.getenv('LOCALAPPDATA', Path.home()/'AppData'/'Local'))/'ZenBridge'/'agent-suite'


def python_path(profile, base=None):
    if profile not in PROFILES:
        raise ValueError('Unknown agent profile')
    return (base or suite_root())/profile/('Scripts/python.exe' if os.name=='nt' else 'bin/python')


def install(profile, base):
    folder=base/profile
    py=python_path(profile,base)
    if not py.exists():
        subprocess.run([sys.executable,'-m','venv',str(folder)],check=True)
    env=dict(os.environ, HF_HUB_DISABLE_TELEMETRY='1', LANGSMITH_TRACING='false', LANGCHAIN_TRACING_V2='false')
    if profile!='speech':
        subprocess.run([str(py),'-m','pip','install',*CPU_TORCH,'--index-url',
                        'https://download.pytorch.org/whl/cpu','--extra-index-url','https://pypi.org/simple'],check=True,env=env)
    subprocess.run([str(py),'-m','pip','install',*PROFILES[profile]],check=True,env=env)
    subprocess.run([str(py),'-m','pip','check'],check=True,env=env)
    frozen=subprocess.check_output([str(py),'-m','pip','freeze'],env=env,text=True)
    (folder/'installed-lock.txt').write_text(frozen,encoding='utf-8')
    record={'profile':profile,'installed_at':time.time(),'requested':PROFILES[profile],
            'lock_sha256':hashlib.sha256(frozen.encode()).hexdigest(),'python':str(py)}
    (folder/'installation.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',choices=['all',*PROFILES],default='all')
    parser.add_argument('--root',type=Path,default=suite_root())
    args=parser.parse_args()
    args.root.mkdir(parents=True,exist_ok=True)
    profiles=PROFILES if args.profile=='all' else [args.profile]
    for profile in profiles:
        install(profile,args.root)


if __name__=='__main__':
    raise SystemExit(main())
