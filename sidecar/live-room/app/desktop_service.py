"""App-owned web service; a closed control pipe stops the server cleanly."""
import os
import sys
import threading
from pathlib import Path
import uvicorn
from app.settings import Settings, fill_process_environ
from app.desktop_update import cleanup_downloads

def main():
    from app.console import safe_console
    safe_console()
    if sys.stdin.readline().strip() != 'start':
        return
    cleanup_downloads()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    fill_process_environ(root / '.env')
    settings = Settings.from_env()
    # round3 C7: rotating, redacted logs/desktop.log (5 MB x 5); ZEN_LOG=off disables
    from app.logfile import install_file_log
    install_file_log('desktop')
    # round3 C1: backup + retention in the desktop build (no admin process there)
    maint = None
    if (os.getenv('ZEN_DESKTOP_MAINT') or '1').strip() != '0':
        from app.desktop_maint import DesktopMaintenance
        maint = DesktopMaintenance.from_env().start()
    server = uvicorn.Server(uvicorn.Config('app.server:app', host='0.0.0.0', port=settings.port, log_level='warning', proxy_headers=False))
    def control():
        sys.stdin.readline()
        server.should_exit = True
    threading.Thread(target=control, daemon=True).start()
    try:
        server.run()
    finally:
        if maint is not None:
            maint.stop()

if __name__ == '__main__':
    main()
