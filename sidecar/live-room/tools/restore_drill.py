#!/usr/bin/env python3
"""備份還原演練（restore drill）：備份 → 驗證 → 還原到暫存 → 比對筆數。

  python tools/restore_drill.py                       # 對 ZEN_DB_PATH 現做一份備份再演練
  python tools/restore_drill.py --latest              # 演練 ZEN_BACKUP_DIR 裡最新的一份
  python tools/restore_drill.py --from-backup X.sqlite3 [--identity-backup Y.sqlite3]
  python tools/restore_drill.py --json                # 只輸出 JSON 報告

不會動正式資料：正式庫只被讀（VACUUM INTO 快照）；還原目標一律在 --work-dir（預設新建暫存資料夾）
裡，與正式路徑相同時拒絕執行。演練產物保留不刪，路徑印在報告裡。報告只有表名、筆數、
檔名、SHA256 與耗時，不含字幕內容或個資。

結束碼：0 = PASS，1 = FAIL（驗證或比對不過），2 = 無法執行（參數、找不到檔案）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.admin import db as zdb  # noqa: E402


class DrillError(Exception):
    """無法執行演練（不是驗證失敗）。"""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def table_counts(path: Path) -> dict[str, int]:
    """每張表（含 FTS 虛擬表、不含 sqlite_ 內部表）的筆數；唯讀開啟。"""
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as c:
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out = {}
        for n in names:
            try:
                out[n] = c.execute('SELECT COUNT(*) FROM "' + n.replace('"', '""') + '"').fetchone()[0]  # CISO P2-1: quote the name
            except sqlite3.DatabaseError as exc:     # 例如 vec0 未載入 extension
                out[n] = f"ERROR: {exc}"
        return out


def user_version(path: Path) -> int:
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


def _diff(a: dict, b: dict) -> dict:
    keys = sorted(set(a) | set(b))
    return {k: {"expected": a.get(k), "actual": b.get(k)} for k in keys if a.get(k) != b.get(k)}


def _manifest_for(backup: Path) -> dict | None:
    m = backup.with_suffix(".json")
    if not m.exists():
        return None
    try:
        return json.loads(m.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _check_manifest(backup: Path, manifest: dict | None, key: str, problems: list[str]) -> None:
    if not manifest:
        return
    entry = (manifest.get("files") or {}).get(key)
    if not entry:
        return
    if entry.get("name") != backup.name:
        return
    if entry.get("sha256") and entry["sha256"] != _sha256(backup):
        problems.append(f"{key} 備份的 SHA256 與 manifest 不符")
    if entry.get("bytes") is not None and entry["bytes"] != backup.stat().st_size:
        problems.append(f"{key} 備份大小與 manifest 不符")


def _guard_target(target: Path, protected: list[Path]) -> None:
    t = target.resolve()
    for p in protected:
        if p is not None and Path(p).resolve() == t:
            raise DrillError(f"還原目標不能是正式資料庫：{t}")


def run_drill(*, db_path: Path, identity_path: Path | None, backup_dir: Path, work_dir: Path,
              from_backup: Path | None = None, identity_backup: Path | None = None,
              latest: bool = False) -> dict:
    t0 = time.perf_counter()
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"tool": "restore_drill", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "work_dir": str(work_dir), "steps": {}, "problems": []}
    problems: list[str] = report["problems"]
    fresh = False

    # 1. 取得備份
    s = time.perf_counter()
    if latest and from_backup is None:
        found = zdb.list_backups(backup_dir)
        if not found:
            raise DrillError(f"備份資料夾裡沒有備份：{backup_dir}")
        from_backup = found[-1]
    if from_backup is not None:
        main_bk = Path(from_backup)
        if not main_bk.exists():
            raise DrillError(f"找不到備份檔：{main_bk}")
        manifest = _manifest_for(main_bk)
        if identity_backup is None and manifest and "identity" in (manifest.get("files") or {}):
            cand = main_bk.parent / manifest["files"]["identity"]["name"]
            identity_backup = cand if cand.exists() else None
            if not cand.exists():
                problems.append("manifest 列了個資庫備份，但檔案不存在")
        ident_bk = Path(identity_backup) if identity_backup else None
        if ident_bk is not None and not ident_bk.exists():
            raise DrillError(f"找不到個資庫備份檔：{ident_bk}")
    else:
        if not Path(db_path).exists():
            raise DrillError(f"找不到主資料庫：{db_path}")
        fresh = True
        out = zdb.backup_set(db_path, work_dir / "backups",
                             identity_path if identity_path and Path(identity_path).exists() else None)
        main_bk = work_dir / "backups" / out["file"]
        ident_bk = work_dir / "backups" / out["files"]["identity"] if "identity" in out["files"] else None
        manifest = _manifest_for(main_bk)
    report["steps"]["backup"] = {"fresh": fresh, "main": main_bk.name,
                                 "identity": ident_bk.name if ident_bk else None,
                                 "manifest": bool(manifest), "ms": int((time.perf_counter() - s) * 1000)}

    # 2. 驗證備份
    s = time.perf_counter()
    for key, bk in (("main", main_bk), ("identity", ident_bk)):
        if bk is None:
            continue
        try:
            zdb._verify_copy(bk)
        except (zdb.SchemaError, sqlite3.DatabaseError) as exc:
            problems.append(f"{key} 備份驗證失敗：{exc}")
        _check_manifest(bk, manifest, key, problems)
    report["steps"]["verify"] = {"ok": not problems, "ms": int((time.perf_counter() - s) * 1000)}
    if problems:
        return _finish(report, t0)

    # 3. 還原到暫存
    s = time.perf_counter()
    restore_dir = work_dir / "restore"
    targets = {"main": restore_dir / "zen.sqlite3", "identity": restore_dir / "zen-identity.sqlite3"}
    protected = [Path(db_path), Path(identity_path) if identity_path else None]
    restored = {}
    for key, bk in (("main", main_bk), ("identity", ident_bk)):
        if bk is None:
            continue
        _guard_target(targets[key], protected)
        try:
            restored[key] = zdb.restore_from(bk, targets[key])
        except (zdb.SchemaError, sqlite3.DatabaseError) as exc:
            problems.append(f"{key} 還原失敗：{exc}")
    report["steps"]["restore"] = {"targets": {k: str(targets[k]) for k in restored},
                                  "ms": int((time.perf_counter() - s) * 1000)}
    if problems:
        return _finish(report, t0)

    # 4. 比對筆數與版本
    s = time.perf_counter()
    compare = {}
    for key, bk in (("main", main_bk), ("identity", ident_bk)):
        if bk is None:
            continue
        want, got = table_counts(bk), table_counts(targets[key])
        d = _diff(want, got)
        if d:
            problems.append(f"{key} 還原後筆數與備份不符：{sorted(d)}")
        if user_version(bk) != user_version(targets[key]):
            problems.append(f"{key} 還原後 user_version 不符")
        errs = sorted(k for k, v in got.items() if isinstance(v, str))
        if errs:
            problems.append(f"{key} 有表無法計數：{errs}")
        entry = {"tables": len(got), "rows": sum(v for v in got.values() if isinstance(v, int)),
                 "user_version": user_version(targets[key]), "mismatch": d}
        live = Path(db_path) if key == "main" else (Path(identity_path) if identity_path else None)
        if fresh and live is not None and live.exists():
            # 正式庫在備份後可能還有寫入：只報差異，不判失敗
            now = table_counts(live)
            entry["live_delta_since_backup"] = {k: (now.get(k) if isinstance(now.get(k), int) else 0)
                                                - (want.get(k) if isinstance(want.get(k), int) else 0)
                                                for k in sorted(set(now) | set(want))
                                                if now.get(k) != want.get(k)}
        compare[key] = entry
    report["steps"]["compare"] = {**compare, "ms": int((time.perf_counter() - s) * 1000)}
    return _finish(report, t0)


def _finish(report: dict, t0: float) -> dict:
    report["result"] = "FAIL" if report["problems"] else "PASS"
    report["total_ms"] = int((time.perf_counter() - t0) * 1000)
    path = Path(report["work_dir"]) / "drill-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    report["report_file"] = str(path)
    return report


def _human(r: dict) -> str:
    lines = [f"還原演練結果：{r['result']}（{r['total_ms']} ms）", f"演練資料夾：{r['work_dir']}"]
    b = r["steps"].get("backup", {})
    lines.append(f"備份：{b.get('main')}" + (f"＋{b['identity']}" if b.get("identity") else "（無個資庫）")
                 + ("（本次新做）" if b.get("fresh") else "（既有）"))
    for key in ("main", "identity"):
        c = r["steps"].get("compare", {}).get(key)
        if c:
            lines.append(f"{key}：{c['tables']} 張表、{c['rows']} 筆，user_version {c['user_version']}，"
                         + ("筆數一致" if not c["mismatch"] else "筆數不一致"))
    for p in r["problems"]:
        lines.append(f"問題：{p}")
    lines.append(f"報告：{r['report_file']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python tools/restore_drill.py", description="備份還原演練")
    ap.add_argument("--db", type=Path, default=None, help="主資料庫（預設 ZEN_DB_PATH）")
    ap.add_argument("--identity", type=Path, default=None, help="個資庫（預設 ZEN_IDENTITY_DB_PATH）")
    ap.add_argument("--backup-dir", type=Path, default=None, help="備份資料夾（預設 ZEN_BACKUP_DIR）")
    ap.add_argument("--from-backup", type=Path, default=None, help="演練這份既有備份，不另做新備份")
    ap.add_argument("--identity-backup", type=Path, default=None, help="搭配的個資庫備份")
    ap.add_argument("--latest", action="store_true", help="演練備份資料夾裡最新的一份")
    ap.add_argument("--work-dir", type=Path, default=None, help="演練產物放這裡（預設新建暫存資料夾）")
    ap.add_argument("--json", action="store_true", help="只輸出 JSON")
    a = ap.parse_args(argv)
    db_path = a.db or zdb.default_db_path()
    identity = a.identity or zdb.identity_db_path()
    work = a.work_dir or Path(tempfile.mkdtemp(prefix="zen-restore-drill-"))
    try:
        r = run_drill(db_path=db_path, identity_path=identity, backup_dir=a.backup_dir or zdb.backup_dir(),
                      work_dir=work, from_backup=a.from_backup, identity_backup=a.identity_backup,
                      latest=a.latest)
    except DrillError as exc:
        print(f"無法執行演練：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(r, ensure_ascii=False, indent=1) if a.json else _human(r))
    return 0 if r["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
