import hashlib
import io
import tarfile
import pytest
from scripts.fetch_draft_model import stage,extract


def fixture(data=b'model'):
    return {'name':'fixed-model','files':{'encoder.onnx':{'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}}}


def test_local_checksum_mismatch_preserves_destination(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'encoder.onnx').write_bytes(b'wrong')
    destination=tmp_path/'destination'
    with pytest.raises(ValueError,match='checksum'):
        stage(destination,source,fixture())
    assert not destination.exists()


def test_validated_copy_and_existing_user_model_are_preserved(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'encoder.onnx').write_bytes(b'model')
    destination=tmp_path/'destination'
    stage(destination,source,fixture())
    assert (destination/'encoder.onnx').read_bytes()==b'model'
    (destination/'encoder.onnx').write_bytes(b'user content')
    with pytest.raises(ValueError,match='Existing'):
        stage(destination,source,fixture())
    assert (destination/'encoder.onnx').read_bytes()==b'user content'


def test_archive_never_extracts_unlisted_paths_or_links(tmp_path):
    archive=tmp_path/'model.tar.bz2';output=tmp_path/'output';output.mkdir()
    with tarfile.open(archive,'w:bz2') as tar:
        bad=tarfile.TarInfo('../escape');bad.size=5;tar.addfile(bad,io.BytesIO(b'model'))
        good=tarfile.TarInfo('fixed-model/encoder.onnx');good.size=5;tar.addfile(good,io.BytesIO(b'model'))
    extract(archive,output,fixture())
    assert not (tmp_path/'escape').exists()
    assert (output/'encoder.onnx').read_bytes()==b'model'
    (output/'encoder.onnx').unlink()
    with tarfile.open(archive,'w:bz2') as tar:
        link=tarfile.TarInfo('fixed-model/encoder.onnx');link.type=tarfile.SYMTYPE;link.linkname='../escape'
        tar.addfile(link)
    with pytest.raises(ValueError,match='Invalid'):
        extract(archive,output,fixture())
