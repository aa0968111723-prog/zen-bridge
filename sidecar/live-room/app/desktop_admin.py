"""Launch the loopback console with the installed App's configuration."""
import os
from pathlib import Path
from app.settings import fill_process_environ, parse_env_file

def embed_from_file(file_env: dict) -> str:
    """The old launcher forced embedding off. An explicit 0 in .env still wins."""
    return "0" if str(file_env.get("ZEN_EMBED") or "").strip() == "0" else "1"

def main():
    root = Path(__file__).resolve().parents[1]
    fill_process_environ(root / '.env')
    os.environ["ZEN_EMBED"] = embed_from_file(parse_env_file(root / '.env'))
    from app.admin.run import main as run
    run()

if __name__ == '__main__':
    main()
