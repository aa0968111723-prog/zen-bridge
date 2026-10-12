"""Install the pinned optional SenseVoice model using the shared verified stager."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.fetch_draft_model import stage

def main():
    manifest=json.loads((ROOT/'app/fast-final-model-manifest.json').read_text())
    local=Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData/Local'))/'ZenBridge/models/fast-final'/manifest['name']
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dest',type=Path,default=local)
    parser.add_argument('--source',type=Path)
    args=parser.parse_args()
    source=args.source or (local if local.is_dir() else None)
    print('Verified SenseVoice model:',stage(args.dest,source,manifest))

if __name__=='__main__':main()
