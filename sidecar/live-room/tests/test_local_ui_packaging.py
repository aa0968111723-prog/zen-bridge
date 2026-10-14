"""A Windows Git checkout must reproduce the installed runtime pins."""
import hashlib
import json
from scripts.build_local_ui import REPO, ROOT, runtime_file_bytes


def test_checked_in_runtime_files_match_pins_with_both_git_line_endings():
    pins = json.loads((ROOT / 'desktop/desktop-manifest.json').read_text())['local_ui']['files']
    for name in ('package.json', 'pnpm-lock.yaml'):
        data = (REPO / 'desktop-runtime' / name).read_bytes().replace(b'\r\n', b'\n')
        for checkout in (data, data.replace(b'\n', b'\r\n')):
            result = runtime_file_bytes(name, checkout)
            assert len(result) == pins['zen-ui/' + name]['size']
            assert hashlib.sha256(result).hexdigest() == pins['zen-ui/' + name]['sha256']
