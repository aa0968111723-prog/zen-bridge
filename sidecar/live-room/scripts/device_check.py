#!/usr/bin/env python3
"""Host-side helper for the device acceptance checklist.

Run this on the machine that serves the room (loopback can read the host
token). It never prints that token.

  python scripts/device_check.py --base http://127.0.0.1:8780 watch --every 10 --seconds 60 --out data/device-watch
  python scripts/device_check.py --base http://127.0.0.1:8780 export --room class --out data/device-export
  python scripts/device_check.py --base http://127.0.0.1:8780 listen --room class --seconds 30 --out data/device-listen.jsonl

``websockets`` is optional. ``listen`` says so and exits 0 when it is missing.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STAMP = re.compile(
    r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$"
)


def redact(text: str, token: str | None) -> str:
    if not token or not text:
        return text
    return text.replace(token, "[redacted]")


def _ms(hours: str, minutes: str, seconds: str, millis: str) -> int:
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def parse_srt(text: str) -> tuple[list[dict], list[str]]:
    """Split an SRT file into cues. Malformed blocks are returned as problems, not cues."""
    problems: list[str] = []
    body = (text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return [], problems
    cues: list[dict] = []
    for block in re.split(r"\n\s*\n", body):
        lines = [line for line in block.split("\n")]
        if not lines or not any(line.strip() for line in lines):
            continue
        head = lines[0].strip()
        if not head.isdigit() or len(lines) < 2:
            problems.append(f"無法解析的區塊：{head[:40]}")
            continue
        stamp = lines[1].strip()
        matched = STAMP.match(stamp)
        if not matched:
            problems.append(f"時間格式不對：{stamp[:60]}")
            cues.append({"index": int(head), "start_ms": None, "end_ms": None, "text": "\n".join(lines[2:])})
            continue
        cues.append({
            "index": int(head),
            "start_ms": _ms(*matched.group(1, 2, 3, 4)),
            "end_ms": _ms(*matched.group(5, 6, 7, 8)),
            "text": "\n".join(lines[2:]).strip(),
        })
    return cues, problems


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def validate_srt(text: str) -> dict:
    """Pure SRT checks: indices, HH:MM:SS,mmm, monotonic starts, no overlap, end > start."""
    cues, problems = parse_srt(text)
    checks: list[dict] = []
    if problems:
        checks.append(_check("timestamps", False, "；".join(problems[:8])))
    else:
        checks.append(_check("timestamps", True, f"{len(cues)} 個時間軸都是 HH:MM:SS,mmm"))
    expected = list(range(1, len(cues) + 1))
    indexes = [cue["index"] for cue in cues]
    if indexes == expected:
        checks.append(_check("indices", True, "沒有 cue" if not cues else f"編號 1..{len(cues)} 連續"))
    else:
        checks.append(_check("indices", False, f"編號是 {indexes[:12]}，應為從 1 連續"))
    bad_span = [
        str(cue["index"])
        for cue in cues
        if cue["start_ms"] is None or cue["end_ms"] is None or cue["end_ms"] <= cue["start_ms"]
    ]
    checks.append(_check(
        "end_after_start",
        not bad_span and not problems,
        "每一段結束都晚於開始" if not bad_span else "結束沒有晚於開始：" + ",".join(bad_span[:12]),
    ))
    backwards = []
    overlap = []
    previous = None
    for cue in cues:
        if previous and cue["start_ms"] is not None and previous["start_ms"] is not None:
            if cue["start_ms"] < previous["start_ms"]:
                backwards.append(f"{previous['index']}→{cue['index']}")
            if previous["end_ms"] is not None and cue["start_ms"] < previous["end_ms"]:
                overlap.append(f"{previous['index']}→{cue['index']}")
        previous = cue
    checks.append(_check(
        "monotonic",
        not backwards,
        "開始時間沒有倒退" if not backwards else "開始時間倒退：" + ",".join(backwards[:12]),
    ))
    checks.append(_check(
        "no_overlap",
        not overlap,
        "沒有重疊" if not overlap else "相鄰 cue 重疊：" + ",".join(overlap[:12]),
    ))
    return {"checks": checks, "cues": cues}


def validate_rows(rows: list) -> dict:
    """Seq uniqueness and the list of missing seq numbers per session."""
    if not isinstance(rows, list):
        return {"checks": [
            _check("seq_unique", False, "匯出 JSON 不是陣列"),
            _check("gaps", False, "無法列出缺號"),
        ]}
    seen: dict[tuple, int] = {}
    by_session: dict[str, set[int]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or "")
        try:
            seq = int(item.get("seq"))
        except (TypeError, ValueError):
            continue
        key = (session_id, seq)
        seen[key] = seen.get(key, 0) + 1
        by_session.setdefault(session_id, set()).add(seq)
    dupes = [f"{sid}:{seq}×{count}" for (sid, seq), count in sorted(seen.items()) if count > 1]
    gaps: list[str] = []
    for session_id, seqs in sorted(by_session.items()):
        if not seqs:
            continue
        missing = [str(number) for number in range(1, max(seqs) + 1) if number not in seqs]
        if missing:
            label = session_id or "(無 session)"
            gaps.append(f"{label} 缺 {','.join(missing[:30])}")
    return {"checks": [
        _check("seq_unique", not dupes, "每個 (session, seq) 只出現一次" if not dupes else "重複：" + ",".join(dupes[:12])),
        _check("gaps", not gaps, "沒有缺號" if not gaps else "；".join(gaps[:8])),
    ]}


def format_report(checks: list[dict], token: str | None = None) -> str:
    lines = []
    failed = 0
    for check in checks:
        mark = "PASS" if check["ok"] else "FAIL"
        if not check["ok"]:
            failed += 1
        lines.append(f"{mark}  {check['name']}: {check['detail']}")
    lines.append(f"整體 {'FAIL' if failed else 'PASS'}（{failed} 項失敗）")
    return redact("\n".join(lines), token)


class HostClient:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.token = ""

    def _open(self, url: str, timeout: float):
        headers = {"cache-control": "no-store", "pragma": "no-cache"}
        if self.token:
            headers["authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def fetch_token(self) -> None:
        status, raw = self._open(self.base + "/api/host-token", 5)
        if status == 403:
            raise SystemExit("拿不到主持權杖（403）。請在主持機本機對 127.0.0.1 執行，不要從別的裝置跑。")
        if status != 200:
            raise SystemExit(f"拿不到主持權杖（HTTP {status}）。請確認服務已在 {self.base} 啟動。")
        try:
            payload = json.loads(raw.decode("utf-8"))
            token = payload.get("token")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            token = None
        if not isinstance(token, str) or not token:
            raise SystemExit("主持權杖回應無法解讀。沒有印出內容。")
        self.token = token

    def get_json(self, path: str, timeout: float = 10):
        status, raw = self._open(self.base + path, timeout)
        if status == 401:
            self.fetch_token()
            status, raw = self._open(self.base + path, timeout)
        text = raw.decode("utf-8", errors="replace")
        if status != 200:
            raise SystemExit(redact(f"GET {path} 失敗（HTTP {status}）", self.token))
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(redact(f"GET {path} 不是 JSON：{exc}", self.token)) from exc

    def get_text(self, path: str, timeout: float = 20) -> str:
        status, raw = self._open(self.base + path, timeout)
        if status == 401:
            self.fetch_token()
            status, raw = self._open(self.base + path, timeout)
        if status != 200:
            raise SystemExit(redact(f"GET {path} 失敗（HTTP {status}）", self.token))
        return raw.decode("utf-8", errors="replace")


def _sample(client: HostClient, room: str) -> dict:
    setup = client.get_json("/api/setup?room_id=" + urllib.request.quote(room))
    metrics = client.get_json("/api/metrics")
    queue = setup.get("queue") if isinstance(setup.get("queue"), dict) else {}
    return {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pending": metrics.get("pending", queue.get("pending")),
        "oldest_wait_ms": metrics.get("oldest_wait_ms", queue.get("oldest_wait_ms")),
        "rejected": metrics.get("rejected", queue.get("rejected")),
        "missing": metrics.get("missing", queue.get("missing")),
        "rss_bytes": metrics.get("rss_bytes"),
        "translate_queued": metrics.get("translate_queued", queue.get("translate_queued")),
        "translate_skipped": metrics.get("translate_skipped", queue.get("translate_skipped")),
        "held": metrics.get("held", queue.get("held")),
        "listeners": metrics.get("listeners", setup.get("listeners")),
        "storage": setup.get("storage"),
        "storage_recovered": setup.get("storage_recovered"),
        "asr_mode": setup.get("asr_mode"),
        "asr_ready": setup.get("asr_ready"),
        "translate_configured": setup.get("translate_configured"),
    }


def cmd_watch(client: HostClient, args) -> int:
    prefix = Path(args.out)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    csv_path = prefix.with_suffix(".csv") if prefix.suffix.lower() != ".csv" else prefix
    json_path = csv_path.with_suffix(".json")
    rows: list[dict] = []
    deadline = time.monotonic() + args.seconds if args.seconds > 0 else None
    fields = [
        "time", "pending", "oldest_wait_ms", "rejected", "missing", "rss_bytes",
        "translate_queued", "translate_skipped", "held", "listeners", "storage",
        "storage_recovered", "asr_mode", "asr_ready", "translate_configured",
    ]
    print(f"每 {args.every:g} 秒記錄一次。輸出 {csv_path} 與 {json_path}。Ctrl-C 結束。")
    try:
        while True:
            row = _sample(client, args.room)
            rows.append(row)
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            print(
                f"{row['time']}  pending={row['pending']} rejected={row['rejected']} "
                f"missing={row['missing']} rss={row['rss_bytes']} translate_queued={row['translate_queued']}"
            )
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(max(0.2, float(args.every)))
    except KeyboardInterrupt:
        print("已停止記錄。")
    return 0


def cmd_export(client: HostClient, args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    room = args.room
    quoted = urllib.request.quote(room)
    raw_json = client.get_text(f"/api/export?room_id={quoted}&kind=json")
    raw_srt = client.get_text(f"/api/export?room_id={quoted}&kind=srt")
    (out / f"{room}.json").write_text(raw_json, encoding="utf-8")
    (out / f"{room}.srt").write_text(raw_srt, encoding="utf-8")
    try:
        rows = json.loads(raw_json)
    except json.JSONDecodeError:
        rows = None
    checks = validate_srt(raw_srt)["checks"] + validate_rows(rows)["checks"]
    report = format_report(checks, client.token)
    (out / "report.txt").write_text(report + "\n", encoding="utf-8")
    print(report)
    print(f"已存 {out / (room + '.json')}、{out / (room + '.srt')}、{out / 'report.txt'}")
    return 0 if all(item["ok"] for item in checks) else 1


def _listen_summary(events: list[dict]) -> str:
    first_zh: dict[str, float] = {}
    first_en: dict[str, float] = {}
    t1: dict[str, object] = {}
    for event in events:
        seg_id = str(event.get("id") or "")
        if not seg_id:
            continue
        arrived = float(event.get("arrived_at") or 0)
        if event.get("t1_ms") is not None:
            t1.setdefault(seg_id, event.get("t1_ms"))
        if event.get("zh") and seg_id not in first_zh:
            first_zh[seg_id] = arrived
        if event.get("en") and seg_id not in first_en:
            first_en[seg_id] = arrived
    lines = [f"收到 {len(events)} 則字幕事件，{len(set(first_zh) | set(first_en))} 個 id。"]
    lines.append("t1_ms 是相對該場開始的毫秒，不是牆鐘。下面的延遲是中文到達到英文到達的差。")
    for seg_id in sorted(set(first_zh) | set(first_en)):
        zh_at = first_zh.get(seg_id)
        en_at = first_en.get(seg_id)
        if zh_at is not None and en_at is not None and en_at >= zh_at:
            gap = int((en_at - zh_at) * 1000)
            gap_text = f"中文到英文 {gap} ms"
        elif zh_at is not None and en_at is not None:
            gap_text = "英文比中文先到（同一則或順序異常）"
        elif en_at is None:
            gap_text = "尚未看到英文"
        else:
            gap_text = "尚未看到中文"
        lines.append(f"{seg_id}  t1_ms={t1.get(seg_id)}  {gap_text}")
    return "\n".join(lines)


def _listen_key_from_setup(setup: dict) -> str:
    key = setup.get("listen_key")
    if isinstance(key, str) and key:
        return key
    url = setup.get("listen_url")
    if not isinstance(url, str) or not url:
        return ""
    found = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("k") or []
    return found[0] if found else ""


def cmd_listen(client: HostClient, args) -> int:
    try:
        import websockets  # type: ignore
    except ImportError:
        print("未安裝 websockets，略過聽眾連線。這個套件不是驗收必備；裝了之後才能記錄字幕到達時間。")
        return 0
    import asyncio

    client.fetch_token()
    setup = client.get_json("/api/setup?room_id=" + urllib.parse.quote(args.room))
    key = _listen_key_from_setup(setup if isinstance(setup, dict) else {})
    if not key:
        print("這個房間沒有聽眾金鑰。請先在主持頁開房。不會改走匿名連線。")
        return 1
    base = client.base
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = "ws://" + base
    url = (
        ws_base
        + "/ws/listen?room_id="
        + urllib.parse.quote(args.room)
        + "&cursor=0&replay=1&k="
        + urllib.parse.quote(key)
    )
    events: list[dict] = []

    async def run() -> None:
        async with websockets.connect(url, open_timeout=5) as socket:
            deadline = time.monotonic() + max(0.5, float(args.seconds))
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "ping":
                    await socket.send(json.dumps({"type": "pong"}))
                    continue
                arrived = time.time()
                if data.get("type") == "hello":
                    for item in list(data.get("backfill") or []) + list(data.get("history") or []) + list(data.get("events") or []):
                        if isinstance(item, dict) and item.get("id"):
                            row = dict(item)
                            row["arrived_at"] = arrived
                            events.append(row)
                    continue
                if data.get("id"):
                    row = dict(data)
                    row["arrived_at"] = arrived
                    events.append(row)

    try:
        asyncio.run(run())
    except Exception as exc:
        print(f"聽眾連線失敗：{type(exc).__name__}。WebSocket 只帶了聽眾金鑰，沒有送出主持權杖。")
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    summary = _listen_summary(events)
    print(summary)
    print(f"已存 {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="主持機實機驗收輔助。不會印出主持權杖。")
    parser.add_argument("--base", default="http://127.0.0.1:8780")
    sub = parser.add_subparsers(dest="cmd", required=True)
    watch = sub.add_parser("watch", help="每隔 N 秒把 /api/setup 與 /api/metrics 寫進 CSV/JSON")
    watch.add_argument("--every", type=float, default=10)
    watch.add_argument("--seconds", type=float, default=0, help="0 表示一直記錄到 Ctrl-C")
    watch.add_argument("--room", default="class")
    watch.add_argument("--out", required=True)
    export = sub.add_parser("export", help="下載 JSON/SRT 並逐項印 PASS/FAIL")
    export.add_argument("--room", default="class")
    export.add_argument("--out", required=True)
    listen = sub.add_parser("listen", help="以聽眾身份連 /ws/listen，記錄每則字幕的到達時間")
    listen.add_argument("--room", default="class")
    listen.add_argument("--seconds", type=float, default=30)
    listen.add_argument("--out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = HostClient(args.base)
    if args.cmd == "listen":
        return cmd_listen(client, args)
    client.fetch_token()
    if args.cmd == "watch":
        return cmd_watch(client, args)
    if args.cmd == "export":
        return cmd_export(client, args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
