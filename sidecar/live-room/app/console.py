"""Windows consoles default to cp1252/cp950: printing Chinese must never crash a CLI (QA dbtest P2)."""
import sys


def safe_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
