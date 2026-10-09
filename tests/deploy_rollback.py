import importlib.util
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location('deploy', Path(__file__).parents[1] / 'scripts/deploy-tencent.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
revision = 'a' * 40
previous = {'spec': {'template': {'spec': {'containers': [{'name': 'zen-bridge', 'image': 'existing', 'env': [{'name': 'EXISTING', 'value': 'preserved'}]}]}}}}
for mode in ('rollout','database'):
    patches = []
    with tempfile.TemporaryDirectory() as directory:
        deploy.BASE = Path(directory)
        (deploy.BASE / 'releases' / revision / 'node_modules').mkdir(parents=True)
        (deploy.BASE / 'incoming').mkdir()
        payload = deploy.BASE / 'payload'
        payload.mkdir()
        (payload / 'REVISION').write_text(revision)
        (payload / 'pnpm-lock.yaml').write_bytes(b'lock\n')
        with tarfile.open(deploy.BASE / 'incoming' / (revision + '.tgz'), 'w:gz') as archive:
            archive.add(payload, arcname='.')
        def kubectl(*args, value=None):
            if 'rollout' in args and mode == 'rollout': raise subprocess.CalledProcessError(1, args)
            if 'patch' in args and 'deployment' in args: patches.append(json.loads(args[-1]))
            if 'get' in args and 'deployment' in args: return json.dumps(previous).encode()
            if 'get' in args and 'pods' in args: return json.dumps({'items': [{'metadata': {'name': 'pod', 'annotations': {'zen.bridge/revision': revision}}, 'status': {'phase': 'Running'}}]}).encode()
            if 'exec' in args and args[-1]=='/zen/scripts/application-readiness.mjs': raise subprocess.CalledProcessError(1, args)
            if 'exec' in args: return deploy.hashlib.sha256(b'lock\n').hexdigest().encode()
            if 'get' in args and 'ingress' in args: return json.dumps({'metadata': {'name': 'ingress'}, 'spec': {'rules': [{'http': {'paths': []}}]}}).encode()
            return b''
        deploy.kubectl = kubectl
        try: deploy.apply(revision)
        except RuntimeError as error: assert 'Readiness failed' in str(error)
        else: raise AssertionError('Expected readiness failure')
        assert patches[0]['spec']['template'] != previous['spec']['template']
        assert patches[-1]['spec'] == previous['spec'], 'rollback must restore the original container and environment'
print('PASS: failed rollout or application database readiness restores original deployment configuration')
