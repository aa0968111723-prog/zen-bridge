"""Forced-command entry point: accepts one bounded release, never a shell."""
import os
import re
import subprocess
import sys
from pathlib import Path
parts=os.getenv('SSH_ORIGINAL_COMMAND','').split()
if len(parts)!=2 or parts[0]!='publish' or not re.fullmatch('[a-f0-9]{40}',parts[1]):
    raise SystemExit('Only publish <revision> is allowed')
target=Path('/opt/zen-bridge/incoming')/(parts[1]+'.tgz')
with target.open('wb') as output:
    total=0
    while block:=sys.stdin.buffer.read(1024*1024):
        total+=len(block)
        if total>350*1024**2:
            raise SystemExit('Release too large')
        output.write(block)
result=subprocess.run(['sudo','-n','python3','/opt/zen-bridge/bin/deploy-tencent.py',parts[1]])
raise SystemExit(result.returncode)
