import hashlib
import pytest
from scripts.download_agent_models import verify_file


def test_weight_digest_and_git_metadata_are_both_verified(tmp_path):
    file=tmp_path/'model';file.write_bytes(b'approved model')
    data=file.read_bytes();size=len(data)
    verify_file(file,size,hashlib.sha256(data).hexdigest())
    verify_file(file,size,blob_id=hashlib.sha1(f'blob {size}\0'.encode()+data).hexdigest())
    file.write_bytes(b'corrupt! model')
    with pytest.raises(ValueError):
        verify_file(file,size,hashlib.sha256(data).hexdigest())
    with pytest.raises(ValueError):
        verify_file(file,size,blob_id=hashlib.sha1(f'blob {size}\0'.encode()+data).hexdigest())
