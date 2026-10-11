"""Download allowlisted gated models using the user's locally cached HF login."""
import argparse
import json
import os
from pathlib import Path
import sys

MODELS={'community1':('pyannote/speaker-diarization-community-1','diarization/community1'),
        'cometkiwi':('Unbabel/wmt22-cometkiwi-da','evaluation/cometkiwi')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model',choices=MODELS)
    args=parser.parse_args()
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    from huggingface_hub import HfApi,get_token,snapshot_download
    if not get_token():
        print(json.dumps({'status':'hf_login_required','model':args.model}))
        return 2
    repo,folder=MODELS[args.model]
    info=HfApi().model_info(repo,files_metadata=True)
    root=Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'ZenBridge/models'/folder
    root.mkdir(parents=True,exist_ok=True)
    snapshot_download(repo_id=repo,revision=info.sha,local_dir=str(root))
    metadata={'repo':repo,'revision':info.sha,'license':getattr(info.card_data,'license',None),
              'status':'downloaded'}
    (root/'zen-model-origin.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    print(json.dumps(metadata))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
