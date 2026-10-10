import hashlib
import io
import json
import zipfile

import pytest
from scripts.fetch_vulkan_runtime import stage

def package(name='whisper-server.exe', source='fixed-source'):
    entries = {name:b'native fixture','build-manifest.json':json.dumps({'source':source}).encode()}
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        for path,data in entries.items():
            archive.writestr(path,data)
    raw=output.getvalue()
    return raw,{'source':'fixed-source','size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
                'files':{name:{'size':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in entries.items()}}

@pytest.mark.parametrize('name',['../outside.dll','..','C:outside.dll','folder\\outside.dll'])
def test_rejects_unsafe_names_before_writing(tmp_path,name):
    raw,spec=package(name)
    with pytest.raises(ValueError):
        stage(spec,tmp_path/'runtime',raw)
    assert not (tmp_path/'runtime').exists()

def test_wrong_source_is_rejected_before_writing(tmp_path):
    raw,spec=package(source='different-source')
    with pytest.raises(ValueError,match='source revision'):
        stage(spec,tmp_path/'runtime',raw)
    assert not (tmp_path/'runtime').exists()

def test_corrupt_member_does_not_replace_existing_runtime(tmp_path):
    raw,spec=package()
    target=tmp_path/'runtime'
    target.mkdir()
    existing=target/'whisper-server.exe'
    existing.write_bytes(b'previous-good')
    spec['files']['whisper-server.exe']['sha256']='0'*64
    with pytest.raises(ValueError,match='file checksum'):
        stage(spec,target,raw)
    assert existing.read_bytes()==b'previous-good'
