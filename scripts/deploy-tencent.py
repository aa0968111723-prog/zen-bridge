"""Host-side release apply. Invoked by a restricted deployment SSH key."""
import json
import copy
import hashlib
import os
import re
import secrets
import subprocess
import sys
import tarfile
import time
from pathlib import Path

BASE = Path('/opt/zen-bridge')
NAMESPACE = 'environment-6ab4cf1136d2a6cac4cddef5'
SERVICE = 'service-6abe121cc3a8364ca8506194'

def kubectl(*args, value=None):
    return subprocess.run(['k3s','kubectl',*args], input=json.dumps(value).encode() if value is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout

def apply(revision):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('Invalid revision')
    archive = BASE / 'incoming' / (revision + '.tgz')
    release = BASE / 'releases' / revision
    release.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if sum(member.size for member in members) > 350 * 1024**2:
            raise ValueError('Release too large')
        for member in members:
            if member.issym() or member.islnk() or Path(member.name).is_absolute() or '..' in Path(member.name).parts:
                raise ValueError('Unsafe release path')
        source.extractall(release, filter='data')
    if (release / 'REVISION').read_text().strip() != revision:
        raise ValueError('Archive revision mismatch')
    modules = release / 'node_modules'
    if not modules.exists() and not modules.is_symlink():
        modules.symlink_to('/src/node_modules', target_is_directory=True)
    previous = json.loads(kubectl('-n',NAMESPACE,'get','deployment',SERVICE,'-o','json'))
    pods=json.loads(kubectl('-n',NAMESPACE,'get','pods','-l','zeabur_service_id='+SERVICE.removeprefix('service-'),'-o','json'))['items']
    running=next(p['metadata']['name'] for p in pods if p['status']['phase']=='Running')
    old_lock=kubectl('-n',NAMESPACE,'exec',running,'--','node','-e',
        'const fs=require("fs"),c=require("crypto");console.log(c.createHash("sha256").update(fs.readFileSync("/src/pnpm-lock.yaml")).digest("hex"))').decode().strip()
    new_lock=hashlib.sha256((release/'pnpm-lock.yaml').read_bytes().replace(b'\r\n',b'\n')).hexdigest()
    if old_lock != new_lock:
        raise RuntimeError('Runtime dependencies changed; build a matching image before deployment')
    backup_dir = BASE / 'private'
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup = backup_dir / ('deployment-' + str(int(time.time())) + '.json')
    backup.write_text(json.dumps(previous))
    backup.chmod(0o600)
    token_path = backup_dir / 'agent-token'
    if not token_path.exists():
        token_path.write_text(secrets.token_hex(32))
        token_path.chmod(0o600)
    token = token_path.read_text().strip()
    kubectl('apply','-f','-', value={'apiVersion':'v1','kind':'Secret','metadata':{'name':'zen-breeze-pairing','namespace':NAMESPACE},
        'type':'Opaque','stringData':{'token':token}})
    template = copy.deepcopy(previous['spec']['template'])
    container = next(c for c in template['spec']['containers'] if c['name']=='zen-bridge')
    env = {item['name']:item for item in container.get('env',[])}
    for name,value in {'PUBLIC_BASE_URL':'https://vexlark.co','SPEECH_PROVIDER':'breeze',
            'BREEZE_ASR_URL':'http://127.0.0.1:8770/transcribe','BREEZE_AGENT_PORT':'8770',
            'ZEN_BRIDGE_REVISION':revision}.items():
        env[name]={'name':name,'value':value}
    env['BREEZE_AGENT_TOKEN']={'name':'BREEZE_AGENT_TOKEN','valueFrom':{'secretKeyRef':{'name':'zen-breeze-pairing','key':'token'}}}
    container.update(command=['node','--experimental-strip-types','/zen/scripts/start.mjs','--zeabur'],args=[],workingDir='/zen',env=list(env.values()))
    mounts = [m for m in container.get('volumeMounts',[]) if m['name']!='zen-release']
    container['volumeMounts']=mounts+[{'name':'zen-release','mountPath':'/zen','readOnly':True}]
    volumes=[v for v in template['spec'].get('volumes',[]) if v['name']!='zen-release']
    template['spec']['volumes']=volumes+[{'name':'zen-release','hostPath':{'path':str(release),'type':'Directory'}}]
    template.setdefault('metadata',{}).setdefault('annotations',{})['zen.bridge/revision']=revision
    kubectl('-n',NAMESPACE,'patch','deployment',SERVICE,'--type=merge','-p',json.dumps({'spec':{'template':template}}))
    kubectl('apply','-f','-', value={'apiVersion':'v1','kind':'Service','metadata':{'name':'zen-breeze-agent','namespace':NAMESPACE},
        'spec':{'selector':{'zeabur_service_id':SERVICE.removeprefix('service-'),'zeabur_type':'user-service'},
                'ports':[{'name':'agent','port':8770,'targetPort':8770}]}})
    ingress=json.loads(kubectl('-n',NAMESPACE,'get','ingress','domain-6abe128528c11572419b5837','-o','json'))
    paths=ingress['spec']['rules'][0]['http']['paths']
    paths=[path for path in paths if path['path']!='/asr-agent']
    paths.insert(0,{'path':'/asr-agent','pathType':'Prefix','backend':{'service':{'name':'zen-breeze-agent','port':{'number':8770}}}})
    kubectl('-n',NAMESPACE,'patch','ingress',ingress['metadata']['name'],'--type=json','-p',json.dumps([
        {'op':'replace','path':'/spec/rules/0/http/paths','value':paths}]))
    try:
        kubectl('-n',NAMESPACE,'rollout','status','deployment/'+SERVICE,'--timeout=240s')
    except subprocess.CalledProcessError:
        kubectl('-n',NAMESPACE,'patch','deployment',SERVICE,'--type=merge','-p',json.dumps({'spec':previous['spec']}))
        raise RuntimeError('Readiness failed; previous service configuration restored')
    print(json.dumps({'revision':revision,'url':'https://vexlark.co','backup':str(backup),'status':'ready'}))

if __name__=='__main__':
    apply(sys.argv[1])
