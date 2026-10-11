"""訓練資料命令列：python -m app.training <指令>

  speakers                         列出講者與已錄分鐘數
  add-speaker 暱稱 [--accent 台語腔]
  consent spk_xxx --by 主持人 [--asr] [--mt] [--live] --confirm
  withdraw spk_xxx --yes           撤回並刪除該講者所有音檔與含他的匯出
  sync-prompts [--zen-db PATH]     載入內建提示句＋詞表（唯讀）
  import AUDIO TRANSCRIPT [--map 標籤=spk_xxx ...]
  import --scan                    匯入 training\\import\\inbox 中同檔名的音檔＋逐字稿
  build [--out DIR] [--zen-db PATH] [--include-unverified] [--max-cer 0.5] [--min-snr 10]
  exports                          列出匯出
資料位置：%LOCALAPPDATA%\\ZenBridge\\training（ZEN_TRAINING_DIR 可覆寫）。全部只在本機。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.training import dataset, importer, prompts, registry
from app.training import db as tdb


def _zen_db(arg):
    if arg:
        return Path(arg)
    from app.admin.db import default_db_path
    p = Path(default_db_path())
    return p if p.exists() else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.training", description="禪譯本機訓練資料工具")
    ap.add_argument("--root", help="訓練資料夾（預設 %%LOCALAPPDATA%%\\ZenBridge\\training）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("speakers")
    a = sub.add_parser("add-speaker"); a.add_argument("label"); a.add_argument("--accent")
    a = sub.add_parser("consent"); a.add_argument("speaker"); a.add_argument("--by", required=True)
    a.add_argument("--asr", action="store_true"); a.add_argument("--mt", action="store_true")
    a.add_argument("--live", action="store_true"); a.add_argument("--date"); a.add_argument("--confirm", action="store_true")
    a = sub.add_parser("withdraw"); a.add_argument("speaker"); a.add_argument("--yes", action="store_true")
    a = sub.add_parser("sync-prompts"); a.add_argument("--zen-db")
    a = sub.add_parser("import"); a.add_argument("audio", nargs="?"); a.add_argument("transcript", nargs="?")
    a.add_argument("--map", action="append", default=[]); a.add_argument("--scan", action="store_true")
    a.add_argument("--title")
    a = sub.add_parser("build"); a.add_argument("--out"); a.add_argument("--zen-db")
    a.add_argument("--include-unverified", action="store_true"); a.add_argument("--max-cer", type=float, default=0.5)
    a.add_argument("--min-snr", type=float, default=10.0); a.add_argument("--seed", default="zen-ft-v1")
    a.add_argument("--no-tm", action="store_true")
    sub.add_parser("exports")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else tdb.training_dir()
    c = tdb.connect(root)
    out = None
    try:
        if args.cmd == "speakers":
            out = registry.list_speakers(c, include_withdrawn=True)
        elif args.cmd == "add-speaker":
            out = registry.new_speaker(c, args.label, accent_note=args.accent, actor="cli")
        elif args.cmd == "consent":
            print(registry.CONSENT_TEXT, file=sys.stderr)
            out = registry.record_consent(c, args.speaker, scope_asr=args.asr, scope_mt=args.mt, harvest_live=args.live,
                                          consent_date=args.date, recorded_by=args.by, confirm_text=args.confirm)
        elif args.cmd == "withdraw":
            if not args.yes:
                print("撤回會永久刪除這位講者的錄音與相關匯出；確認請加 --yes", file=sys.stderr)
                return 2
            out = registry.withdraw(c, root, args.speaker, actor="cli")
        elif args.cmd == "sync-prompts":
            out = {"added": prompts.sync(c, _zen_db(args.zen_db))}
        elif args.cmd == "import":
            if args.scan:
                out = importer.scan_inbox(c, root)
            else:
                if not (args.audio and args.transcript):
                    print("需要 AUDIO 與 TRANSCRIPT，或用 --scan", file=sys.stderr)
                    return 2
                m = dict(x.split("=", 1) for x in args.map)
                out = importer.import_recording(c, root, Path(args.audio), Path(args.transcript), title=args.title,
                                                speaker_map=m, actor="cli")
        elif args.cmd == "build":
            out = dataset.build(c, root, out_dir=Path(args.out) if args.out else None, actor="cli", options={
                "include_unverified": args.include_unverified, "max_cer": args.max_cer, "min_snr_db": args.min_snr,
                "seed": args.seed, "include_tm": not args.no_tm, "zen_db": str(_zen_db(args.zen_db) or "") or None})
        elif args.cmd == "exports":
            out = [dict(r) for r in c.execute("SELECT * FROM exports ORDER BY created_at DESC")]
    except registry.RegistryError as exc:
        print(f"錯誤：{exc.detail}", file=sys.stderr)
        return 1
    finally:
        c.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
