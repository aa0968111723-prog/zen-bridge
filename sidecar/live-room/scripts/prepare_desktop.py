"""Create a relocatable, offline Windows App payload from verified inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.package_release import source_files
from scripts.install_runtime import download_verified, extract_binaries, verified

def prepare(output, assets, sdk=None):
    output.mkdir(parents=True, exist_ok=True)
    for path in source_files():
        relative = path.relative_to(ROOT)
        if relative.parts[0] == 'desktop' and relative.suffix.lower() in {'.exe', '.dll'}:
            continue
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    cache = ROOT / '.downloads'
    cache.mkdir(exist_ok=True)
    asset = json.loads((ROOT / 'bootstrap-manifest.json').read_text())['python']
    archive = cache / ('python-' + asset['version'] + '.zip')
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != asset['sha256']:
        with urllib.request.urlopen(asset['url'], timeout=60) as incoming, archive.open('wb') as outgoing:
            shutil.copyfileobj(incoming, outgoing)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != asset['sha256']:
        raise RuntimeError('Python checksum mismatch')
    python_root = output / '.python' / asset['version']
    python = python_root / 'tools/python.exe'
    if not python.exists():
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(python_root)
    for pth in python_root.glob('tools/python*._pth'):
        lines = [line for line in pth.read_text(encoding='utf-8').splitlines() if line.strip() != '#import site']
        if not any(line.strip() == 'import site' for line in lines):
            lines.append('import site')
        if not any('site-packages' in line for line in lines):
            lines.append(r'Lib\site-packages')
        pth.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    environment = os.environ.copy()
    environment.pop('SSLKEYLOGFILE', None)
    environment['PYTHONUTF8'] = '1'
    # Packages stay inside this interpreter. A venv records absolute paths and
    # breaks when the installer copies the App to another folder.
    subprocess.run([str(python), '-m', 'ensurepip', '--upgrade'], env=environment, check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir', '-r', str(ROOT / 'requirements-lock.txt')], env=environment, check=True)
    manifest = json.loads((ROOT / 'runtime-manifest.json').read_text())['assets']
    model_asset = manifest['model']
    model_source = assets / 'models' / model_asset['name']
    if not verified(model_source, model_asset):
        model_source = download_verified(model_asset, cache / model_asset['name'])
    model_target = output / 'models' / model_asset['name']
    model_target.parent.mkdir(exist_ok=True)
    if not verified(model_target, model_asset):
        shutil.copy2(model_source, model_target)
    model_target.with_suffix('.bin.sha256').write_text(model_asset['sha256']+'\n')
    for name in ('whisper', 'ffmpeg'):
        tool_asset = manifest[name]
        tool_archive = assets / '.downloads' / tool_asset['name']
        if not verified(tool_archive, tool_asset):
            tool_archive = download_verified(tool_asset, cache / tool_asset['name'])
        extract_binaries(tool_archive, output / 'tools')
    command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'desktop/build.ps1'), '-OutputDirectory', str(output)]
    if sdk:
        command += ['-SdkDirectory', str(sdk)]
    subprocess.run(command, check=True)
    # Libraries ship with their licenses; no user configuration enters payload.
    notices = output / 'desktop/THIRD-PARTY.txt'
    notices.write_text('Python: PSF license (.python/<version>/tools/LICENSE.txt)\n'
        'whisper.cpp / pywhispercpp: MIT; https://github.com/ggml-org/whisper.cpp and https://github.com/absadiki/pywhispercpp\n'
        'FFmpeg Gyan essentials: GPL build; source and license: https://www.gyan.dev/ffmpeg/builds/ and https://ffmpeg.org/legal.html\n'
        'Breeze-ASR: https://huggingface.co/MediaTek-Research/Breeze-ASR-25\n'
        'Microsoft WebView2 SDK/runtime: Microsoft license; https://aka.ms/webview2\n', encoding='utf-8')
    print('Offline App payload ready:', output)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--assets', type=Path, default=ROOT)
    parser.add_argument('--sdk', type=Path)
    args = parser.parse_args()
    prepare(args.output.resolve(), args.assets.resolve(), args.sdk.resolve() if args.sdk else None)
