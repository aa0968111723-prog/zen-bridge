param([switch]$SkipShortcut)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path $PSScriptRoot 'tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
$env:SSLKEYLOGFILE = $null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Get-BreezeSha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $hash.Dispose() }
}
try {
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        throw 'Windows x64 Intel/AMD is required.'
    }
    $manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'bootstrap-manifest.json') -Raw | ConvertFrom-Json
    $asset = $manifest.python
    $pythonRoot = Join-Path $PSScriptRoot ('.python\' + $asset.version)
    $python = Join-Path $pythonRoot 'tools\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        $cache = Join-Path $PSScriptRoot '.downloads'
        New-Item -ItemType Directory -Force $cache | Out-Null
        $archive = Join-Path $cache ('python-' + $asset.version + '.zip')
        Write-Host 'Preparing private Python runtime...'
        if (-not (Test-Path -LiteralPath $archive) -or (Get-BreezeSha256 $archive) -ne $asset.sha256) {
            Invoke-WebRequest -UseBasicParsing -Uri $asset.url -OutFile ($archive + '.part')
            if ((Get-BreezeSha256 ($archive + '.part')) -ne $asset.sha256) {
                throw 'Python download checksum mismatch. Run install again.'
            }
            Move-Item -LiteralPath ($archive + '.part') -Destination $archive -Force
        }
        $stage = Join-Path $cache ('python-stage-' + [Guid]::NewGuid().ToString('N'))
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($archive, $stage)
        New-Item -ItemType Directory -Force (Split-Path $pythonRoot) | Out-Null
        Move-Item -LiteralPath $stage -Destination $pythonRoot
    }
    & $python -c "import sys; assert sys.version_info[:2] == (3, 12)"
    if ($LASTEXITCODE -ne 0) { throw 'Private Python runtime is damaged.' }
    & $python -c "from pathlib import Path; from scripts.update import require_stopped; require_stopped(Path.cwd())"
    if ($LASTEXITCODE -ne 0) { throw 'Close Breeze before installing or repairing.' }
    $venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $needsEnvironment = $true
    if (Test-Path -LiteralPath $venvPython) {
        & $venvPython -c "import sys, pip; from pathlib import Path; assert Path(sys.base_prefix).resolve() == Path(sys.argv[1]).resolve()" (Join-Path $pythonRoot 'tools')
        $needsEnvironment = $LASTEXITCODE -ne 0
    }
    if ($needsEnvironment) {
        & $python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Cannot create the Python environment.' }
    }
    & .\.venv\Scripts\python.exe -m pip install --disable-pip-version-check --no-cache-dir -r requirements-lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    if (-not (Test-Path (Join-Path $env:SystemRoot 'System32\msvcp140.dll')) -or
        -not (Test-Path (Join-Path $env:SystemRoot 'System32\vcruntime140.dll'))) {
        $vcInstaller = Join-Path $PSScriptRoot '.downloads\vc_redist.x64.exe'
        New-Item -ItemType Directory -Force (Split-Path $vcInstaller) | Out-Null
        Write-Host 'Preparing Microsoft Visual C++ x64 runtime...'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://aka.ms/vc14/vc_redist.x64.exe' -OutFile $vcInstaller
        $signature = Get-AuthenticodeSignature -LiteralPath $vcInstaller
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
            throw 'Microsoft runtime signature verification failed.'
        }
        $vcProcess = Start-Process -FilePath $vcInstaller -ArgumentList '/install', '/quiet', '/norestart' -WindowStyle Hidden -Wait -PassThru
        if ($vcProcess.ExitCode -notin @(0, 1638, 3010)) { throw ('Microsoft runtime installation failed: ' + $vcProcess.ExitCode) }
        if ($vcProcess.ExitCode -eq 3010) { Write-Host 'Windows restart is recommended by the Microsoft runtime installer.' }
    }
    if ($SkipShortcut) { $env:BREEZE_SKIP_SHORTCUT = '1' }
    & .\.venv\Scripts\python.exe scripts\install_runtime.py
    if ($LASTEXITCODE -ne 0) { throw 'Model/tool installation or preflight failed.' }
    Write-Host 'Breeze installation completed. Double-click start.bat.'
    exit 0
} catch {
    Write-Host ('Installation failed: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
