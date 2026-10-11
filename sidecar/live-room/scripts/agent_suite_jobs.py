"""Explicit local optional jobs; the model cannot choose program or file actions."""
import argparse
import json
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.agent_suite import call_worker,suite_status


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['status','enhance','diarize','evaluate'])
    parser.add_argument('--input',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.operation=='status':
        print(json.dumps(suite_status(),ensure_ascii=False));return
    if not args.input or not args.input.is_file():
        parser.error('--input must be an existing file')
    if not args.output or args.output.exists():
        parser.error('--output must be a new file')
    models=Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'ZenBridge/models'
    if args.operation=='enhance':
        result=call_worker('enhance',{'input_wav':str(args.input.resolve()),'output_wav':str(args.output.resolve()),
                                    'model':str(models/'enhancement/gtcrn_simple.onnx')})
    else:
        if args.operation=='diarize':
            payload={'input_wav':str(args.input.resolve()),'model_dir':str(models/'diarization/community1')}
        else:
            if args.input.stat().st_size>256000:
                parser.error('Evaluation file exceeds 256 KB')
            rows=[json.loads(line) for line in args.input.read_text(encoding='utf-8').splitlines() if line.strip()]
            payload={'rows':rows,'checkpoint':str(models/'evaluation/cometkiwi/checkpoints/model.ckpt')}
        result=call_worker(args.operation,payload)
        with args.output.open('x',encoding='utf-8') as output:
            json.dump(result,output,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
