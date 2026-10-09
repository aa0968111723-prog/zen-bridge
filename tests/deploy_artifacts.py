import importlib.util
import tempfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('deploy',Path(__file__).parents[1]/'scripts/deploy-tencent.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)
with tempfile.TemporaryDirectory() as directory:
    deploy.BASE=Path(directory)
    releases=deploy.BASE/'releases';incoming=deploy.BASE/'incoming'
    releases.mkdir();incoming.mkdir()
    current,prior,old,foreign='a'*40,'b'*40,'c'*40,'d'*40
    for revision in (current,prior,old,foreign):
        folder=releases/revision;folder.mkdir()
        (folder/'REVISION').write_text(revision if revision!=foreign else 'unmanaged')
        (incoming/(revision+'.tgz')).write_bytes(b'generated artifact')
    (releases/'personal').mkdir();(incoming/'personal.txt').write_text('keep')
    result=deploy.prune_release_artifacts({current,prior})
    assert result=={'releases':1,'archives':1}
    assert all((releases/name).is_dir() for name in (current,prior,foreign,'personal'))
    assert all((incoming/(name+'.tgz')).is_file() for name in (current,prior,foreign))
    assert (incoming/'personal.txt').read_text()=='keep'
print('PASS: artifact pruning retains current, rollback and unmanaged folders within the managed directories')
