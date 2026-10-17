"""Tests for tools/model_file_manifest.py (queue item 37).

Every model folder is a tmp_path fixture of small fake files; manifests are read
and written with explicit UTF-8.  Nothing depends on real models, network, ports,
POSIX permission bits, path separators, locale or the clock.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "model_file_manifest.py"

sys.path.insert(0, str(ROOT / "tools"))
import model_file_manifest as mfm  # noqa: E402

FILES = {
    "asr/ggml-breeze-asr-25-q5_0.bin": b"ggml" + bytes(range(256)) * 9,
    "asr/config.json": '{"name": "breeze-asr-25", "語言": "zh"}'.encode("utf-8"),
    "mt/qwen2.5-1.5b-instruct-q4_k_m.gguf": b"GGUF" + b"\x01\x02\x03" * 700,
    "vad/silero_vad.onnx": b"onnx-silero" * 40,
    "README.txt": "模型說明\n".encode("utf-8"),
}
VERIFY_KEYS = {
    "tool", "schema_version", "mode", "status", "exit_code", "quick", "allow_extra", "root", "manifest",
    "algorithm", "chunk_size", "counts", "bytes", "disk", "missing", "size_mismatch", "sha256_mismatch",
    "unreadable", "extra", "case_mismatch", "refetch", "links", "issues", "suggestions", "summary_zh",
}


# --------------------------------------------------------------------------- fixtures
def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def models(tmp_path: Path) -> Path:
    root = tmp_path / "models"
    for rel, data in FILES.items():
        _write(root.joinpath(*rel.split("/")), data)
    return root


@pytest.fixture
def manifest(models: Path, tmp_path: Path) -> Path:
    out = tmp_path / "models.manifest.json"
    assert mfm.main(["generate", "--root", str(models), "--out", str(out)]) == 0
    return out


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _verify(models: Path, manifest: Path, *extra: str) -> tuple[int, dict]:
    out = manifest.parent / f"report-{len(list(manifest.parent.glob('report-*.json')))}.json"
    code = mfm.main(["verify", "--root", str(models), "--manifest", str(manifest), "--json", str(out), *extra])
    return code, _load(out)


def _codes(report: dict) -> set[str]:
    return {i["code"] for i in report["issues"]}


# --------------------------------------------------------------------------- helpers
def test_sha256_file_streams_in_chunks_across_boundaries(tmp_path):
    data = bytes((i * 7) % 251 for i in range(10_000))
    p = _write(tmp_path / "big.bin", data)
    for chunk in (1, 7, 4096, 9_999, 10_000, 1 << 20):
        digest, n = mfm.sha256_file(p, chunk)
        assert n == len(data)
        assert digest == hashlib.sha256(data).hexdigest()
    assert mfm.CHUNK_SIZE == 1024 * 1024


@pytest.mark.parametrize("raw,expected", [
    ("asr/a.bin", "asr/a.bin"),
    ("asr\\sub\\a.bin", "asr/sub/a.bin"),
    ("./asr//a.bin", "asr/a.bin"),
    ("模型/語音.bin", "模型/語音.bin"),
])
def test_normalize_rel_posix(raw, expected):
    assert mfm.normalize_rel(raw) == expected


@pytest.mark.parametrize("raw", ["", "/etc/passwd", "C:/models/a.bin", "C:\\models\\a.bin", "../a.bin",
                                 "asr/../../a.bin", ".", None, 5])
def test_normalize_rel_rejects_unsafe(raw):
    with pytest.raises(ValueError):
        mfm.normalize_rel(raw)


def test_kind_of_guesses_model_family():
    assert "ASR" in mfm.kind_of("asr/ggml-breeze-asr-25-q5_0.bin")
    assert "GGUF" in mfm.kind_of("mt/qwen.gguf")
    assert mfm.kind_of("vad/silero_vad.onnx") == "Silero VAD"
    assert mfm.kind_of("whisper/ggml-silero-v5.1.2.bin") == "Silero VAD"
    assert "設定" in mfm.kind_of("asr/config.json")


def test_fmt_bytes():
    assert mfm.fmt_bytes(0) == "0 B"
    assert mfm.fmt_bytes(1536) == "1.5 KiB"
    assert mfm.fmt_bytes(3 * 1024 ** 3) == "3.0 GiB"
    assert mfm.fmt_bytes(None) == "—"


# --------------------------------------------------------------------------- generate
def test_generate_writes_posix_relative_sorted_manifest(models, manifest):
    doc = _load(manifest)
    assert doc["kind"] == "zen-bridge-model-manifest" and doc["schema_version"] == 1
    assert doc["algorithm"] == "sha256" and doc["chunk_size"] == 1024 * 1024
    assert doc["root_name"] == "models"
    paths = [f["path"] for f in doc["files"]]
    assert paths == sorted(FILES)
    assert all("\\" not in p and not p.startswith("/") for p in paths)
    for f in doc["files"]:
        data = FILES[f["path"]]
        assert f["size"] == len(data)
        assert f["sha256"] == hashlib.sha256(data).hexdigest()
    assert doc["file_count"] == len(FILES)
    assert doc["total_bytes"] == sum(len(v) for v in FILES.values())
    assert "generated_at" not in doc  # deterministic: no clock in the manifest


def test_generate_is_deterministic(models, manifest, tmp_path):
    out2 = tmp_path / "again.json"
    assert mfm.main(["generate", "--root", str(models), "--out", str(out2)]) == 0
    assert manifest.read_bytes() == out2.read_bytes()


def test_generate_summary_zh(models, tmp_path, capsys):
    out = tmp_path / "m.json"
    assert mfm.main(["generate", "--root", str(models), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "模型 manifest 產生：完成" in text and "已寫入新檔" in text and "檔案數：5" in text
    assert "Silero VAD" in text and "GGUF" in text


def test_generate_out_inside_root_is_not_listed(models):
    out = models / "manifest.json"
    assert mfm.main(["generate", "--root", str(models), "--out", str(out)]) == 0
    assert "manifest.json" not in [f["path"] for f in _load(out)["files"]]
    # a later manifest in the same folder does not list the earlier one either
    out2 = models / "manifest-2.json"
    assert mfm.main(["generate", "--root", str(models), "--out", str(out2)]) == 0
    assert [f["path"] for f in _load(out2)["files"]] == sorted(FILES)
    # and verify with the default root (= manifest folder) is clean
    assert mfm.main(["verify", "--manifest", str(out), "--exclude", "manifest-2.json"]) == 0
    assert mfm.main(["verify", "--manifest", str(out2)]) == 0


def test_generate_never_overwrites_out_exit_4(models, tmp_path, capsys):
    out = _write(tmp_path / "m.json", b"keep me")
    assert mfm.main(["generate", "--root", str(models), "--out", str(out)]) == 4
    assert out.read_bytes() == b"keep me"
    assert "拒絕覆蓋" in capsys.readouterr().err


def test_generate_json_report_never_overwrites_exit_4(models, tmp_path):
    rep = _write(tmp_path / "r.json", b"keep")
    out = tmp_path / "m.json"
    assert mfm.main(["generate", "--root", str(models), "--out", str(out), "--json", str(rep)]) == 4
    assert rep.read_bytes() == b"keep"
    assert not out.exists()  # refused before any work


def test_generate_json_report(models, tmp_path):
    rep = tmp_path / "r.json"
    assert mfm.main(["generate", "--root", str(models), "--out", str(tmp_path / "m.json"), "--json", str(rep)]) == 0
    r = _load(rep)
    assert r["mode"] == "generate" and r["status"] == "ok" and r["file_count"] == 5


def test_generate_unwritable_out_exit_4(models, tmp_path):
    out = tmp_path / "no-such-dir" / "m.json"
    assert mfm.main(["generate", "--root", str(models), "--out", str(out)]) == 4
    assert not out.exists()


def test_generate_bad_root_exit_3(tmp_path, capsys):
    assert mfm.main(["generate", "--root", str(tmp_path / "nope"), "--out", str(tmp_path / "m.json")]) == 3
    assert "輸入錯誤" in capsys.readouterr().err
    f = _write(tmp_path / "file.bin", b"x")
    assert mfm.main(["generate", "--root", str(f), "--out", str(tmp_path / "m.json")]) == 3
    empty = tmp_path / "empty"
    empty.mkdir()
    assert mfm.main(["generate", "--root", str(empty), "--out", str(tmp_path / "m.json")]) == 3
    assert not (tmp_path / "m.json").exists()


def test_generate_default_and_custom_excludes(models, tmp_path):
    _write(models / "asr" / "Thumbs.db", b"junk")
    _write(models / ".DS_Store", b"junk")
    _write(models / "mt" / "notes.md", b"n")
    out = tmp_path / "m.json"
    assert mfm.main(["generate", "--root", str(models), "--out", str(out), "--exclude", "*.md"]) == 0
    assert [f["path"] for f in _load(out)["files"]] == sorted(FILES)


# --------------------------------------------------------------------------- verify
def test_verify_ok(models, manifest, capsys):
    code, r = _verify(models, manifest)
    assert code == 0 and r["status"] == "ok" and r["exit_code"] == 0
    assert set(r) == VERIFY_KEYS
    assert r["counts"]["ok"] == 5 and r["counts"]["missing"] == 0 and r["counts"]["extra"] == 0
    assert r["bytes"]["hashed"] == sum(len(v) for v in FILES.values())
    assert r["issues"] == []
    out = capsys.readouterr().out
    assert "模型檔案比對：通過（exit 0）" in out and "所有模型檔都和 manifest 一致" in out
    assert "sha256 全檔串流比對" in out


def test_verify_missing_file_fail(models, manifest, capsys):
    (models / "mt" / "qwen2.5-1.5b-instruct-q4_k_m.gguf").unlink()
    code, r = _verify(models, manifest)
    assert code == 2 and r["status"] == "fail"
    assert [m["path"] for m in r["missing"]] == ["mt/qwen2.5-1.5b-instruct-q4_k_m.gguf"]
    assert "GGUF" in r["missing"][0]["kind"]
    assert r["refetch"] == ["mt/qwen2.5-1.5b-instruct-q4_k_m.gguf"]
    assert r["bytes"]["refetch_needed"] == len(FILES["mt/qwen2.5-1.5b-instruct-q4_k_m.gguf"])
    assert "missing" in _codes(r)
    out = capsys.readouterr().out
    assert "不通過" in out and "缺檔" in out and "重新取得 mt/qwen2.5-1.5b-instruct-q4_k_m.gguf" in out
    assert "建議" in out and "不會下載" in out and "磁碟剩餘" in out


def test_verify_size_mismatch_fail(models, manifest, capsys):
    p = models / "asr" / "ggml-breeze-asr-25-q5_0.bin"
    p.write_bytes(p.read_bytes()[:-100])  # truncated copy
    code, r = _verify(models, manifest)
    assert code == 2 and _codes(r) == {"size_mismatch"}
    bad = r["size_mismatch"][0]
    assert bad["path"] == "asr/ggml-breeze-asr-25-q5_0.bin"
    assert bad["expected_size"] - bad["actual_size"] == 100
    assert r["counts"]["ok"] == 4
    out = capsys.readouterr().out
    assert "大小不符" in out and "檔案不完整" in out and "重新取得 asr/ggml-breeze-asr-25-q5_0.bin" in out


def test_verify_sha_mismatch_fail(models, manifest, capsys):
    p = models / "vad" / "silero_vad.onnx"
    data = bytearray(p.read_bytes())
    data[10] ^= 0xFF  # same size, one flipped byte
    p.write_bytes(bytes(data))
    code, r = _verify(models, manifest)
    assert code == 2 and _codes(r) == {"sha256_mismatch"}
    bad = r["sha256_mismatch"][0]
    assert bad["path"] == "vad/silero_vad.onnx" and bad["kind"] == "Silero VAD"
    assert bad["actual_sha256"] == hashlib.sha256(bytes(data)).hexdigest()
    assert bad["expected_sha256"] == hashlib.sha256(FILES["vad/silero_vad.onnx"]).hexdigest()
    assert "sha256 不符" in capsys.readouterr().out


def test_verify_extra_files_warn_and_partial_download_hint(models, manifest, capsys):
    _write(models / "mt" / "qwen2.5-7b.gguf.part", b"x" * 300)
    _write(models / "asr" / "ggml-old.bin", b"y" * 50)
    code, r = _verify(models, manifest)
    assert code == 1 and r["status"] == "warn" and _codes(r) == {"extra"}
    extras = {x["path"]: x for x in r["extra"]}
    assert set(extras) == {"mt/qwen2.5-7b.gguf.part", "asr/ggml-old.bin"}
    assert extras["mt/qwen2.5-7b.gguf.part"]["partial_download"] is True
    assert extras["asr/ggml-old.bin"]["partial_download"] is False
    assert r["bytes"]["extra_total"] == 350
    out = capsys.readouterr().out
    assert "警告" in out and "多餘" in out and "中斷的下載" in out and "generate" in out


def test_verify_allow_extra_is_ok(models, manifest):
    _write(models / "asr" / "ggml-old.bin", b"y" * 50)
    code, r = _verify(models, manifest, "--allow-extra")
    assert code == 0 and r["counts"]["extra"] == 1 and r["issues"] == []


def test_verify_quick_compares_size_only(models, manifest, capsys):
    p = models / "vad" / "silero_vad.onnx"
    data = bytearray(p.read_bytes())
    data[0] ^= 0x01
    p.write_bytes(bytes(data))
    code, r = _verify(models, manifest, "--quick")
    assert code == 0 and r["quick"] is True and r["bytes"]["hashed"] == 0
    assert r["sha256_mismatch"] == [] and r["counts"]["ok"] == 5
    out = capsys.readouterr().out
    assert "快速模式" in out and "只比大小" in out
    # quick still catches size problems
    p.write_bytes(bytes(data) + b"!")
    code, r = _verify(models, manifest, "--quick")
    assert code == 2 and _codes(r) == {"size_mismatch"}
    # full mode catches the flipped byte
    code, r = _verify(models, manifest)
    assert code == 2


def test_verify_combined_problems(models, manifest):
    (models / "README.txt").unlink()
    (models / "asr" / "config.json").write_bytes(b"{}")
    _write(models / "extra.bin", b"e")
    code, r = _verify(models, manifest)
    assert code == 2
    assert _codes(r) == {"missing", "size_mismatch", "extra"}
    assert r["refetch"] == ["README.txt", "asr/config.json"]


def test_verify_windows_style_manifest_paths(models, tmp_path):
    doc = {"kind": "zen-bridge-model-manifest", "schema_version": 1, "algorithm": "sha256",
           "files": [{"path": rel.replace("/", "\\"), "size": len(d), "sha256": hashlib.sha256(d).hexdigest().upper()}
                     for rel, d in FILES.items()]}
    m = _write(tmp_path / "win.json", json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    code, r = _verify(models, m)
    assert code == 0, r["issues"]


def test_verify_bom_and_utf16_manifest(models, manifest, tmp_path):
    text = manifest.read_text(encoding="utf-8")
    m1 = _write(tmp_path / "bom.json", b"\xef\xbb\xbf" + text.encode("utf-8"))
    m2 = _write(tmp_path / "u16.json", text.encode("utf-16"))
    assert mfm.main(["verify", "--root", str(models), "--manifest", str(m1)]) == 0
    assert mfm.main(["verify", "--root", str(models), "--manifest", str(m2)]) == 0


def test_verify_case_only_difference_warns(models, tmp_path):
    doc = {"files": [{"path": rel.upper() if rel == "README.txt" else rel, "size": len(d),
                      "sha256": hashlib.sha256(d).hexdigest()} for rel, d in FILES.items()]}
    m = _write(tmp_path / "case.json", json.dumps(doc).encode("utf-8"))
    code, r = _verify(models, m)
    assert code == 1 and _codes(r) == {"case_mismatch"}
    assert r["case_mismatch"] == [{"manifest_path": "README.TXT", "disk_path": "README.txt"}]


def test_verify_entry_without_sha_warns(models, tmp_path):
    doc = {"files": [{"path": rel, "size": len(d)} if rel == "README.txt" else
                     {"path": rel, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
                     for rel, d in FILES.items()]}
    m = _write(tmp_path / "nosha.json", json.dumps(doc).encode("utf-8"))
    code, r = _verify(models, m)
    assert code == 1 and _codes(r) == {"unhashed"}


def test_verify_unicode_names_nfc(tmp_path):
    root = tmp_path / "m"
    name = "模型/語音辨識.bin"
    _write(root / "模型" / "語音辨識.bin", "權重".encode("utf-8"))
    out = tmp_path / "m.json"
    assert mfm.main(["generate", "--root", str(root), "--out", str(out)]) == 0
    assert _load(out)["files"][0]["path"] == name
    assert mfm.main(["verify", "--root", str(root), "--manifest", str(out)]) == 0


def test_verify_json_never_overwrites_exit_4(models, manifest, tmp_path, capsys):
    rep = _write(tmp_path / "report.json", b"keep me")
    assert mfm.main(["verify", "--root", str(models), "--manifest", str(manifest), "--json", str(rep)]) == 4
    assert rep.read_bytes() == b"keep me"
    assert "拒絕覆蓋" in capsys.readouterr().err


def test_verify_json_unwritable_exit_4(models, manifest, tmp_path):
    rep = tmp_path / "no-such-dir" / "r.json"
    assert mfm.main(["verify", "--root", str(models), "--manifest", str(manifest), "--json", str(rep)]) == 4


def test_verify_json_not_created_on_input_error(models, tmp_path):
    rep = tmp_path / "r.json"
    assert mfm.main(["verify", "--root", str(models), "--manifest", str(tmp_path / "nope.json"),
                     "--json", str(rep)]) == 3
    assert not rep.exists()


@pytest.mark.parametrize("content", [
    b"",
    b"not json",
    b'{"files": "nope"}',
    b'{"files": []}',
    b'{"kind": "something-else", "files": [{"path": "a", "size": 1}]}',
    b'{"schema_version": 99, "files": [{"path": "a", "size": 1}]}',
    b'{"algorithm": "md5", "files": [{"path": "a", "size": 1}]}',
    b'{"files": [42]}',
    b'{"files": [{"path": "../escape.bin", "size": 1}]}',
    b'{"files": [{"path": "/abs.bin", "size": 1}]}',
    b'{"files": [{"path": "a", "size": -1}]}',
    b'{"files": [{"path": "a", "size": true}]}',
    b'{"files": [{"path": "a", "size": "12"}]}',
    b'{"files": [{"path": "a", "size": 1, "sha256": "xyz"}]}',
    b'{"files": [{"path": "a", "size": 1}, {"path": "a", "size": 1}]}',
    b'{"files": [{"path": "a/b", "size": 1}, {"path": "a\\\\b", "size": 1}]}',
    b"\xff\x00\xfe\x01garbage",
    '{"files": [{"path": "壞", "size": 1}]}'.encode("big5"),
])
def test_bad_manifest_exit_3(models, tmp_path, content, capsys):
    m = _write(tmp_path / "bad.json", content)
    assert mfm.main(["verify", "--root", str(models), "--manifest", str(m)]) == 3
    assert "輸入錯誤" in capsys.readouterr().err


def test_verify_missing_manifest_or_root_exit_3(models, manifest, tmp_path):
    assert mfm.main(["verify", "--root", str(models), "--manifest", str(tmp_path / "nope.json")]) == 3
    assert mfm.main(["verify", "--root", str(models), "--manifest", str(tmp_path)]) == 3
    assert mfm.main(["verify", "--root", str(tmp_path / "nope"), "--manifest", str(manifest)]) == 3


@pytest.mark.parametrize("argv", [
    [],
    ["bogus"],
    ["generate"],
    ["generate", "--root", "m"],
    ["verify"],
    ["verify", "--root", "m"],
    ["verify", "--manifest", "m.json", "--bogus"],
    ["generate", "--root", "m", "--out", "x.json", "--quick"],
    ["generate", "--root", "m", "--out", "same.json", "--json", "same.json"],
    ["verify", "--manifest", "m.json", "--json", "m.json"],
])
def test_argparse_errors_exit_3_not_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        mfm.main(argv)
    assert exc.value.code == 3
    assert "參數錯誤" in capsys.readouterr().err


# --------------------------------------------------------------------------- links
def _symlink(link: Path, target: Path, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:  # Windows without symlink privilege
        pytest.skip(f"symlinks unavailable: {exc}")


def test_symlink_escaping_root_is_skipped_and_warned(models, tmp_path, capsys):
    outside = _write(tmp_path / "outside" / "secret.gguf", b"outside")
    _symlink(models / "mt" / "linked.gguf", outside)
    _symlink(models / "outdir", outside.parent, is_dir=True)
    out = tmp_path / "m.json"
    assert mfm.main(["generate", "--root", str(models), "--out", str(out)]) == 1
    assert [f["path"] for f in _load(out)["files"]] == sorted(FILES)
    text = capsys.readouterr().out
    assert "根目錄外" in text
    code, r = _verify(models, out)
    assert code == 1 and _codes(r) == {"symlink_escape"}
    assert {x["path"] for x in r["links"]["escaped"]} == {"mt/linked.gguf", "outdir"}


def test_symlink_inside_root_is_followed(models, tmp_path):
    _symlink(models / "asr" / "current.bin", models / "asr" / "ggml-breeze-asr-25-q5_0.bin")
    out = tmp_path / "m.json"
    assert mfm.main(["generate", "--root", str(models), "--out", str(out)]) == 0
    files = {f["path"]: f for f in _load(out)["files"]}
    assert files["asr/current.bin"]["sha256"] == files["asr/ggml-breeze-asr-25-q5_0.bin"]["sha256"]


def test_broken_symlink_warned(models, manifest, tmp_path):
    _symlink(models / "dangling.bin", tmp_path / "does-not-exist.bin")
    code, r = _verify(models, manifest)
    assert code == 1 and _codes(r) == {"broken_symlink"}


def test_symlink_loop_does_not_hang(models, manifest):
    _symlink(models / "asr" / "loop", models, is_dir=True)
    code, r = _verify(models, manifest)
    assert code == 0, r["issues"]


# --------------------------------------------------------------------------- read-only / CLI
def test_verify_is_read_only(models, manifest, tmp_path):
    def snapshot() -> dict[str, bytes]:
        return {p.relative_to(models).as_posix(): p.read_bytes() for p in models.rglob("*") if p.is_file()}

    before = snapshot()
    m_before = manifest.read_bytes()
    (models / "README.txt").unlink()
    before.pop("README.txt")
    assert _verify(models, manifest)[0] == 2
    assert snapshot() == before
    assert manifest.read_bytes() == m_before


def test_subprocess_cli_utf8_output(models, manifest):
    proc = subprocess.run(
        [sys.executable, str(TOOL), "verify", "--root", str(models), "--manifest", str(manifest)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "模型檔案比對：通過" in proc.stdout


def test_subprocess_argparse_error_exit_3():
    proc = subprocess.run([sys.executable, str(TOOL), "verify"], capture_output=True, text=True,
                          encoding="utf-8", timeout=60, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 3


# --------------------------------------------------------------------------- hygiene
def test_stdlib_only_and_no_network():
    src = TOOL.read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    stdlib = set(sys.stdlib_module_names)
    assert mods <= stdlib | {"__future__"}, mods - stdlib
    assert not mods & {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "ftplib", "ssl",
                       "subprocess", "asyncio", "smtplib", "xmlrpc", "app"}
    assert "8645" not in src
    for word in ("urlopen", "http://", "https://", "huggingface_hub", "snapshot_download"):
        assert word not in src


def test_shebang_and_docstring_document_exit_codes():
    src = TOOL.read_text(encoding="utf-8")
    assert src.startswith("#!/usr/bin/env python3")
    doc = ast.get_docstring(ast.parse(src)) or ""
    for code in ("``ok``", "``warn``", "``fail``", "(input)", "(output)"):
        assert code in doc
    assert (mfm.EXIT_OK, mfm.EXIT_WARN, mfm.EXIT_FAIL, mfm.EXIT_INPUT, mfm.EXIT_OUTPUT) == (0, 1, 2, 3, 4)
