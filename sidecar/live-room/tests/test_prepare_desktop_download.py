"""NuGet Python download retries a stalled read and does not publish a partial."""
import hashlib

import pytest

from scripts.prepare_desktop import download_python_archive

URL = "https://www.nuget.org/api/v2/package/python/3.12.10"


class Body:
    def __init__(self, data, fail_after=False):
        self.data = data
        self.fail_after = fail_after

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self, size=-1):
        if self.fail_after and self.data == b"":
            raise TimeoutError("The read operation timed out")
        data, self.data = self.data, b""
        return data


def test_timeout_then_success_publishes_only_verified_bytes(tmp_path):
    body = b"python-nupkg"
    digest = hashlib.sha256(body).hexdigest()
    calls = {"n": 0}

    def opener(url, timeout=0):
        assert url == URL and timeout == 60
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("The read operation timed out")
        return Body(body)

    archive = tmp_path / "python-3.12.10.zip"
    saved = download_python_archive(URL, archive, digest, opener=opener, pause=lambda seconds: None)
    assert calls["n"] == 3
    assert saved.read_bytes() == body
    assert not archive.with_name(archive.name + ".part").exists()


def test_stalled_read_does_not_keep_a_partial(tmp_path):
    def opener(url, timeout=0):
        return Body(b"partial", fail_after=True)

    archive = tmp_path / "python-3.12.10.zip"
    with pytest.raises(RuntimeError, match="已重試仍失敗"):
        download_python_archive(URL, archive, "ab" * 32, opener=opener, attempts=2, pause=lambda seconds: None)
    assert not archive.exists()
    assert not archive.with_name(archive.name + ".part").exists()


def test_matching_cache_does_not_download(tmp_path):
    body = b"cached"
    archive = tmp_path / "python.zip"
    archive.write_bytes(body)

    def opener(*args, **kwargs):
        raise AssertionError("should not download")

    saved = download_python_archive(URL, archive, hashlib.sha256(body).hexdigest(), opener=opener, pause=lambda seconds: None)
    assert saved.read_bytes() == body
