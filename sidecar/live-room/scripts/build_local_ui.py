"""Bundle the loopback UI and pinned Node runtime; never copy user configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from scripts.install_runtime import sha256

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]


def stage_local_ui(payload: Path) -> None:
    spec = json.loads((ROOT / 'desktop/desktop-manifest.json').read_text())['local_ui']
    node = Path(os.environ.get('ZEN_NODE_EXE') or shutil.which('node') or '')
    node_spec = spec['files']['runtime/node/node.exe']
    if not node.is_file() or node.stat().st_size != node_spec['size'] or sha256(node) != node_spec['sha256']:
        raise RuntimeError('Build requires the pinned Windows x64 Node ' + spec['node_version'])
    pnpm = shutil.which('pnpm.cmd' if os.name == 'nt' else 'pnpm')
    if not pnpm:
        raise RuntimeError('Install pnpm 11.25.0 before building the local desktop UI')
    env = {**os.environ, 'DEPLOY_TARGET': 'local'}
    # Remove cloud credentials from the build process as well as the package.
    for key in ('DATABASE_URL', 'OPENAI_API_KEY', 'DASHSCOPE_API_KEY', 'HERMES_API_KEY', 'BREEZE_AGENT_TOKEN'):
        env.pop(key, None)
    env['PATH'] = str(node.parent) + os.pathsep + env.get('PATH', '')
    subprocess.run([str(node), '--experimental-strip-types', 'scripts/run-framework.mjs', 'build'],
                   cwd=REPO, env=env, check=True)
    ui = payload / 'zen-ui'
    ui.mkdir(parents=True, exist_ok=True)
    for name in ('dist', 'public', 'drizzle', 'scripts', 'lib', 'dictionary-data'):
        shutil.copytree(REPO / name, ui / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('.env*', '__pycache__', '*.pyc'))
    (ui / 'sidecar').mkdir(exist_ok=True)
    for name in ('breeze-gateway.mjs', 'zen-front.mjs'):
        shutil.copy2(REPO / 'sidecar' / name, ui / 'sidecar' / name)
    for name in ('package.json', 'pnpm-lock.yaml'):
        source = REPO / 'desktop-runtime' / name
        if sha256(source) != spec['files']['zen-ui/' + name]['sha256']:
            raise RuntimeError('Update the desktop manifest when changing runtime dependencies: ' + name)
        shutil.copy2(source, ui / name)
    # Hoisted copy installation has no junctions back into a developer's pnpm store.
    with tempfile.TemporaryDirectory(prefix='zen-ui-deps-') as directory:
        temp = Path(directory)
        for name in ('package.json', 'pnpm-lock.yaml'):
            shutil.copy2(ui / name, temp / name)
        subprocess.run([pnpm, 'install', '--prod', '--frozen-lockfile', '--ignore-scripts',
                        '--node-linker=hoisted', '--package-import-method=copy'],
                       cwd=temp, env=env, check=True, shell=os.name == 'nt')
        shutil.copytree(temp / 'node_modules', ui / 'node_modules', dirs_exist_ok=True)
    target = payload / 'runtime/node'
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(node, target / 'node.exe')
    license_file = node.parent / 'LICENSE'
    if not license_file.is_file():
        raise RuntimeError('Pinned Node distribution must include its LICENSE')
    shutil.copy2(license_file, target / 'LICENSE')
