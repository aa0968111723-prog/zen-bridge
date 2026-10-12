"""Screen-only speech preview. Never writes captions, memory or a transcript."""
import json
import os
from pathlib import Path
from app.fast_final import SherpaOfflineFinal

class PreviewAsr:
    def __init__(self,root):
        self.root=Path(root)
        self.engine=None
        self.failed=False
    def model_directory(self):
        manifest=json.loads((self.root/'app/fast-final-model-manifest.json').read_text())
        for folder in (self.root/'models/fast-final'/manifest['name'],
            Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData/Local'))/'ZenBridge/models/fast-final'/manifest['name']):
            if all((folder/name).is_file() for name in ('model.int8.onnx','tokens.txt')):
                return folder
        return None
    def available(self):
        try:return not self.failed and self.model_directory() is not None
        except (OSError,ValueError,KeyError):return False
    def warmup(self):
        if self.engine is None:
            import psutil
            if psutil.virtual_memory().available<1024**3:
                raise RuntimeError('Insufficient memory for optional preview')
            self.engine=SherpaOfflineFinal('sense_voice',self.model_directory(),threads=1,language='auto')
    def transcribe(self,wav):
        self.warmup()
        return self.engine.transcribe(wav)
