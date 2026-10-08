"""App-owned web service; a closed control pipe stops the server cleanly."""
import os
import sys
import threading
from pathlib import Path
import uvicorn
from app.settings import Settings, fill_process_environ
from app.desktop_update import cleanup_downloads

def main():
    if sys.stdin.readline().strip() != 'start':
        return
    cleanup_downloads()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    fill_process_environ(root / '.env')
    settings = Settings.from_env()
    server = uvicorn.Server(uvicorn.Config('app.server:app', host='0.0.0.0', port=settings.port, log_level='warning'))
    def control():
        sys.stdin.readline()
        server.should_exit = True
    threading.Thread(target=control, daemon=True).start()
    server.run()

if __name__ == '__main__':
    main()
