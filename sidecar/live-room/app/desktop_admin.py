"""Launch the loopback console with the installed App's configuration."""
from pathlib import Path
from app.settings import fill_process_environ

def main():
    root = Path(__file__).resolve().parents[1]
    fill_process_environ(root / '.env')
    from app.admin.run import main as run
    run()

if __name__ == '__main__':
    main()
