"""Opt-in speech extras from the verified isolated speech profile only."""
import json
import os
from pathlib import Path
import sys


def bootstrap():
    if os.getenv('ZEN_AGENT_SUITE_SPEECH','0') != '1':
        return False
    from app.agent_suite import suite_root
    folder=suite_root()/'speech'
    try:
        marker=json.loads((folder/'installation.json').read_text(encoding='utf-8'))
        cfg=(folder/'pyvenv.cfg').read_text(encoding='utf-8')
        version=next(line.split('=',1)[1].strip() for line in cfg.splitlines() if line.startswith('version ='))
        if marker.get('profile') != 'speech' or tuple(map(int,version.split('.')[:2])) != sys.version_info[:2]:
            return False
    except (OSError,ValueError,StopIteration):
        return False
    packages=folder/('Lib/site-packages' if os.name=='nt' else f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages')
    if not all((packages/name).is_dir() for name in ('sherpa_onnx','opencc')):
        return False
    # Existing App dependencies win. Evaluation/diarization packages are never imported here.
    if str(packages) not in sys.path:
        sys.path.append(str(packages))
    return True
