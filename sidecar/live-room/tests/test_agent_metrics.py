import json
import threading
from pathlib import Path

from app.agent_metrics import AgentMetrics

_ALLOWED = {"kind", "elapsed_ms", "ok", "at", "audio_seconds", "rtf", "route", "usage_known"}


def _backup(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".1")


def test_default_window_is_the_newest_hundred():
    assert AgentMetrics().limit == 100
    assert AgentMetrics().snapshot()["persisted_window"] == 100
    assert "保存視窗" in AgentMetrics().snapshot()["persisted_note"]
    assert AgentMetrics().snapshot()["write_failures"] == 0


def test_restart_restores_newest_history_inside_rotated_files(tmp_path):
    path = tmp_path / "agent-metrics.jsonl"
    first = AgentMetrics(limit=4, path=path)
    for i in range(10):
        first.record("transcribe", i, i % 2 == 0, audio_seconds=1)
    live = first.snapshot()
    assert live["completed"] == 5
    assert live["failed"] == 5
    assert [item["elapsed_ms"] for item in live["history"]] == [6, 7, 8, 9]
    assert live["persisted_completed"] == 2
    assert live["persisted_failed"] == 2
    assert "不是全部歷史" in live["persisted_note"]
    names = sorted(item.name for item in tmp_path.iterdir())
    assert names == ["agent-metrics.jsonl", "agent-metrics.jsonl.1"]
    assert len(path.read_text(encoding="utf-8").splitlines()) <= 4
    assert len(_backup(path).read_text(encoding="utf-8").splitlines()) <= 4

    second = AgentMetrics(limit=4, path=path)
    restored = second.snapshot()
    assert [item["elapsed_ms"] for item in restored["history"]] == [6, 7, 8, 9]
    assert restored["completed"] == 0
    assert restored["persisted_completed"] == 2
    assert restored["persisted_failed"] == 2
    assert restored["persisted_window"] == 4


def test_write_failure_is_counted_without_dropping_memory(tmp_path, monkeypatch):
    path = tmp_path / "metrics" / "agent-metrics.jsonl"
    metrics = AgentMetrics(path=path)
    real_open = Path.open

    def fail_open(self, *args, **kwargs):
        if self == path:
            raise OSError("disk full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_open)
    metrics.record("translate", 5, False, route="local", usage_known=False)
    snap = metrics.snapshot()
    assert snap["write_failures"] == 1
    assert snap["history"][0]["route"] == "local"
    assert snap["failed"] == 1
    assert snap["persisted_failed"] == 0, 'a failed write is not a saved event'
    assert not path.exists()


def test_reloaded_lines_drop_captions_and_secrets(tmp_path):
    path = tmp_path / "agent-metrics.jsonl"
    AgentMetrics(path=path).record("transcribe", 1, True, audio_seconds=0.5)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "kind": "transcribe", "elapsed_ms": 2, "ok": True, "at": 1,
            "zh": "課堂字幕", "token": "secret-token", "audio": "AAA",
        }, ensure_ascii=False) + "\n")
    snap = AgentMetrics(path=path).snapshot()
    blob = json.dumps(snap, ensure_ascii=False)
    assert "課堂字幕" not in blob
    assert "secret-token" not in blob
    assert [item["elapsed_ms"] for item in snap["history"]] == [1, 2]
    assert all(set(item) <= _ALLOWED for item in snap["history"])


def test_legacy_file_compacts_to_the_window(tmp_path):
    path = tmp_path / "agent-metrics.jsonl"
    path.write_text("".join(
        json.dumps({"kind": "transcribe", "elapsed_ms": i, "ok": True, "at": i, "zh": "不該留下"}, ensure_ascii=False) + "\n"
        for i in range(10)
    ), encoding="utf-8")
    snap = AgentMetrics(limit=4, path=path).snapshot()
    assert [item["elapsed_ms"] for item in snap["history"]] == [6, 7, 8, 9]
    assert "不該留下" not in path.read_text(encoding="utf-8")
    assert not _backup(path).exists()
    for line in path.read_text(encoding="utf-8").splitlines():
        assert set(json.loads(line)) <= _ALLOWED


def test_concurrent_records_keep_the_window_and_allowed_keys(tmp_path):
    path = tmp_path / "agent-metrics.jsonl"
    metrics = AgentMetrics(limit=100, path=path)

    def run():
        for _ in range(40):
            metrics.record("transcribe", 3, True, audio_seconds=0.5)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    snap = metrics.snapshot()
    assert snap["completed"] == 160
    assert snap["write_failures"] == 0
    assert len(snap["history"]) == 100
    assert snap["persisted_completed"] == 100
    text = path.read_text(encoding="utf-8")
    if _backup(path).exists():
        text += _backup(path).read_text(encoding="utf-8")
    assert "Bearer" not in text
    for line in text.splitlines():
        assert set(json.loads(line)) <= _ALLOWED
