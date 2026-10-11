"""Fetch pinned GTCRN and the explicit Ollama embedding model."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

GTCRN_URL='https://github.com/k2-fsa/sherpa-onnx/releases/download/speech-enhancement-models/gtcrn_simple.onnx'
GTCRN_SHA256='e77603ac0c23dac3227dd2d7135b3a585cbee2679048aecfa886657d3ae1b534'
GTCRN_SIZE=535638


def main():
    local=Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData'/'Local'))
    output=local/'ZenBridge/models/enhancement/gtcrn_simple.onnx'
    output.parent.mkdir(parents=True,exist_ok=True)
    valid=output.is_file() and output.stat().st_size==GTCRN_SIZE and hashlib.sha256(output.read_bytes()).hexdigest()==GTCRN_SHA256
    if not valid:
        with tempfile.TemporaryDirectory(dir=output.parent) as work:
            candidate=Path(work)/'model.onnx'
            with urllib.request.urlopen(GTCRN_URL,timeout=60) as incoming:
                data=incoming.read(GTCRN_SIZE+1)
            if len(data)!=GTCRN_SIZE or hashlib.sha256(data).hexdigest()!=GTCRN_SHA256:
                raise RuntimeError('GTCRN download checksum mismatch')
            candidate.write_bytes(data)
            os.replace(candidate,output)
    ollama=shutil.which('ollama')
    if not ollama:
        candidate=local/'Programs/Ollama/ollama.exe'
        ollama=str(candidate) if candidate.is_file() else None
    if not ollama:
        raise RuntimeError('GTCRN ready; install Ollama before downloading qwen3-embedding:0.6b')
    subprocess.run([ollama,'pull','qwen3-embedding:0.6b'],check=True,timeout=1800)
    print('GTCRN checksum verified; Ollama embedding model ready.')


if __name__=='__main__':
    main()
