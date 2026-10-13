"""Create a relocatable, offline Windows App payload from verified inputs."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.package_release import source_files
from scripts.install_runtime import download_verified, extract_binaries, sha256, verified

def download_python_archive(url, archive, digest, opener=None, attempts=3, pause=None):
    """Retry a stalled NuGet Python download. Publish only after SHA256 matches."""
    if not str(url).startswith('https://'):
        raise ValueError('Python 下載來源必須使用 HTTPS')
    archive = Path(archive)
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.is_file() and sha256(archive) == digest:
        return archive
    open_url = opener or urllib.request.urlopen
    wait = pause if pause is not None else time.sleep
    part = archive.with_name(archive.name + '.part')
    last = None
    for attempt in range(1, attempts + 1):
        try:
            part.unlink(missing_ok=True)
            with open_url(url, timeout=60) as incoming, part.open('wb') as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            if sha256(part) != digest:
                raise RuntimeError('Python checksum mismatch')
            part.replace(archive)
            return archive
        except (TimeoutError, urllib.error.URLError, OSError, RuntimeError) as exc:
            last = exc
            part.unlink(missing_ok=True)
            if attempt >= attempts:
                break
            print(f'Python NuGet 下載未完成，重試 {attempt + 1}/{attempts}', flush=True)
            wait(min(8, 2 * attempt))
    raise RuntimeError('Python NuGet 下載逾時或未通過檢查，已重試仍失敗') from last

def prepare(output, assets, sdk=None):
    # pip's wheel extraction can still hit MAX_PATH on standard Windows installs.
    # Check before downloading or creating a partially installed interpreter.
    if os.name == 'nt':
        longest = output / '.python/3.12.10-zen052/tools/Lib/site-packages/onnxruntime/tools/ort_format_model/ort_flatbuffers_py/fbs/RuntimeOptimizationRecordContainerEntry.py'
        if len(str(longest.resolve())) >= 260:
            raise ValueError('Windows payload path is too long. Use a short build directory such as %LOCALAPPDATA%/ZenBridge/build/payload.')
    environment = os.environ.copy()
    environment.pop('SSLKEYLOGFILE', None)
    environment['PYTHONUTF8'] = '1'
    # Optional native features must have their verified assets staged before
    # enumerating source_files; the resulting installer works offline.
    subprocess.run([sys.executable, str(ROOT / 'scripts/fetch_windows_runtime.py')], env=environment, check=True)
    subprocess.run([sys.executable, str(ROOT / 'scripts/fetch_silero_vad.py')], env=environment, check=True)
    subprocess.run([sys.executable, str(ROOT / 'scripts/fetch_vulkan_runtime.py')], env=environment, check=True)
    from scripts.fetch_draft_model import stage as stage_draft
    draft_manifest=json.loads((ROOT/'app/draft-model-manifest.json').read_text())
    draft_source=assets/'models/draft'/draft_manifest['name']
    if not draft_source.is_dir():
        draft_source=Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'ZenBridge/models/draft'/draft_manifest['name']
    stage_draft(output/'models/draft'/draft_manifest['name'],draft_source if draft_source.is_dir() else None,draft_manifest)
    fast_manifest=json.loads((ROOT/'app/fast-final-model-manifest.json').read_text())
    fast_source=assets/'models/fast-final'/fast_manifest['name']
    if not fast_source.is_dir():
        fast_source=Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData/Local'))/'ZenBridge/models/fast-final'/fast_manifest['name']
    stage_draft(output/'models/fast-final'/fast_manifest['name'],fast_source if fast_source.is_dir() else None,fast_manifest)
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
    download_python_archive(asset['url'], archive, asset['sha256'])
    python_directory = asset.get('directory',asset['version'])
    python_root = output / '.python' / python_directory
    (output/'desktop/offline-runtime.json').write_text(json.dumps({'python_directory':python_directory}),encoding='ascii')
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
    # Vulkan server and every ggml backend come from a single verified build.
    gpu = output / 'tools/whisper-vulkan'
    shutil.copytree(ROOT / 'tools/whisper-vulkan', gpu, dirs_exist_ok=True)
    for runtime_dll in (ROOT / 'app/vendor/msvc').glob('*.dll'):
        shutil.copy2(runtime_dll, gpu / runtime_dll.name)
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
    with notices.open('a', encoding='utf-8') as writer:
        writer.write('sherpa-onnx: Apache-2.0; https://github.com/k2-fsa/sherpa-onnx\n'
                     'OpenCC Python: Apache-2.0; https://github.com/yichen0831/opencc-python\n'
                     'X-ASR: https://github.com/Gilgamesh-J/X-ASR; model provenance: app/draft-model-manifest.json\n')
        writer.write('SenseVoice: FunAudioLLM / Alibaba Group; ONNX export by Fangjun Kuang (sherpa-onnx). '
                     'FunASR Model Open Source License; app/vendor/licenses/SenseVoice-MODEL-LICENSE.txt; '
                     'fixed provenance: app/fast-final-model-manifest.json\n')
        writer.write('Silero VAD v6.2.3: MIT; https://github.com/snakers4/silero-vad\n'
                     'Microsoft Visual C++ runtime 14.50 x64: Microsoft redistributable license; '
                     'extracted from byte-pinned official vc_redist.x64.exe; see app/windows-runtime-manifest.json\n')
    print('Offline App payload ready:', output)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--assets', type=Path, default=ROOT)
    parser.add_argument('--sdk', type=Path)
    args = parser.parse_args()
    prepare(args.output.resolve(), args.assets.resolve(), args.sdk.resolve() if args.sdk else None)
