"""Download allowlisted gated models using the user's locally cached HF login."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

MODELS={'community1':('pyannote/speaker-diarization-community-1','diarization/community1'),
        'cometkiwi':('Unbabel/wmt22-cometkiwi-da','evaluation/cometkiwi')}
REVISIONS={'community1':'3533c8cf8e369892e6b79ff1bf80f7b0286a54ee',
           'cometkiwi':'1ad785194e391eebc6c53e2d0776cada8f83179a'}
FILES={'community1':('README.md','config.yaml','embedding/README.md','embedding/pytorch_model.bin',
                    'plda/README.md','plda/plda.npz','plda/xvec_transform.npz','segmentation/pytorch_model.bin'),
       'cometkiwi':('LICENSE','README.md','hparams.yaml','checkpoints/model.ckpt')}


def verify_file(path,size,lfs_sha256=None,blob_id=None):
    if not path.is_file() or path.stat().st_size!=size:
        raise ValueError('Model file size mismatch')
    digest=hashlib.sha256() if lfs_sha256 else hashlib.sha1()
    if not lfs_sha256:
        digest.update(f'blob {size}\0'.encode())
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            digest.update(block)
    if digest.hexdigest()!=(lfs_sha256 or blob_id):
        raise ValueError('Model file checksum mismatch')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model',choices=MODELS)
    args=parser.parse_args()
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    # Bounded-memory HTTP streaming avoids large Xet reconstruction buffers on Windows.
    if os.name=='nt':
        os.environ.setdefault('HF_HUB_DISABLE_XET','1')
    from huggingface_hub import HfApi,get_token,snapshot_download
    if not get_token():
        print(json.dumps({'status':'hf_login_required','model':args.model}))
        return 2
    repo,folder=MODELS[args.model]
    info=HfApi().model_info(repo,revision=REVISIONS[args.model],files_metadata=True)
    root=Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'ZenBridge/models'/folder
    root.mkdir(parents=True,exist_ok=True)
    snapshot_download(repo_id=repo,revision=info.sha,local_dir=str(root),
                      allow_patterns=list(FILES[args.model]),max_workers=2)
    specs={entry.rfilename:entry for entry in info.siblings}
    verified=[]
    for name in FILES[args.model]:
        entry=specs[name]
        sha=getattr(entry.lfs,'sha256',None)
        verify_file(root/name,entry.size,sha,entry.blob_id)
        verified.append({'name':name,'size':entry.size,'sha256':sha,'git_blob':entry.blob_id})
    metadata={'repo':repo,'revision':info.sha,'license':getattr(info.card_data,'license',None),
              'status':'verified','files':verified}
    (root/'zen-model-origin.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    print(json.dumps(metadata))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
