#Requires -Version 5.1

<#
.SYNOPSIS
  One-time setup for the zen-bridge local backend (Ollama translation, ledger, TM, VAD, admin).
.DESCRIPTION
  Runs inside sidecar\live-room of a git worktree. Steps:
    1. venv (.venv) with Python 3.12 + requirements-lock.txt + requirements-local.txt
    2. ollama pull qwen3:4b (Q4_K_M) and qwen3-embedding:0.6b   (skip with -SkipOllama)
    3. fetch the pinned Silero VAD model and verify sha256      (skip with -SkipVad)
    4. migrate %LOCALAPPDATA%\ZenBridge\data\zen.sqlite3 (+ zen-identity.sqlite3)
  It does NOT turn any feature on. Run scripts\bench_local.ps1 first, then set the env
  flags shown at the end (docs\LOCAL_SETUP.md).
#>
param(
  [string]$Python = "",
  [switch]$SkipOllama,
  [switch]$SkipVad,
  [string]$TranslateModel = "qwen3:4b",
  [string]$EmbedModel = "qwen3-embedding:0.6b"
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$env:PYTHONUTF8 = '1'

function Step($t) { Write-Host "`n== $t ==" -ForegroundColor Cyan }

Step '1/4 Python venv'
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
  if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3.12 -m venv .venv }
    else { & python -m venv .venv }
  } else { & $Python -m venv .venv }
  if ($LASTEXITCODE -ne 0) { throw 'Python venv creation failed' }
}
& $venvPy -c "import sys; assert sys.version_info[:2] >= (3, 11), sys.version"
if ($LASTEXITCODE -ne 0) { throw 'venv Python 版本檢查失敗（需要 3.11 以上）' }
& $venvPy -m pip install --disable-pip-version-check -q -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'pip install -r requirements-lock.txt failed' }
& $venvPy -m pip install --disable-pip-version-check -q -r requirements-local.txt
if ($LASTEXITCODE -ne 0) { throw 'pip install -r requirements-local.txt failed' }
# Codex 2569a06: byte-pinned app-local Microsoft runtime (no system DLL changes)
& $venvPy scripts\fetch_windows_runtime.py
if ($LASTEXITCODE -ne 0) { throw 'App-local Microsoft runtime preparation failed' }

# D-001: the per-user Ollama installer puts ollama.exe in %LOCALAPPDATA%\Programs\Ollama and
# only adds it to the *user* PATH, so shells started before the install (or by agents/
# services) do not see it. Look in the standard install folders before giving up.
function Resolve-Ollama {
  $cmd = Get-Command ollama -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $candidates = @()
  if ($env:OLLAMA_EXE) { $candidates += $env:OLLAMA_EXE }
  if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe') }
  if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles 'Ollama\ollama.exe') }
  foreach ($c in $candidates) { if ($c -and (Test-Path -LiteralPath $c)) { return $c } }
  return $null
}

Step '2/4 Ollama models'
if ($SkipOllama) { Write-Host 'skipped (-SkipOllama)' }
else {
  $ollama = Resolve-Ollama
  if (-not $ollama) {
    Write-Warning '找不到 ollama.exe（PATH、%LOCALAPPDATA%\Programs\Ollama、%ProgramFiles%\Ollama 都沒有）。請先從 https://ollama.com/download 安裝，或設 OLLAMA_EXE，再重跑本腳本。'
  } else {
    Write-Host "ollama: $ollama"
    $failed = @()
    foreach ($m in @($TranslateModel, $EmbedModel)) {
      & $ollama pull $m
      if ($LASTEXITCODE -ne 0) { $failed += $m }
    }
    & $ollama list
    # Codex 2569a06: a failed model pull fails setup instead of only warning.
    if ($failed.Count) { throw ("ollama pull 失敗：{0}。請確認網路後重跑本腳本。" -f ($failed -join ', ')) }
  }
}

Step '3/4 Silero VAD model'
if ($SkipVad) { Write-Host 'skipped (-SkipVad)' }
else {
  & $venvPy scripts\fetch_silero_vad.py
  if ($LASTEXITCODE -ne 0) { Write-Warning "VAD 模型下載或驗證失敗（exit $LASTEXITCODE）。VAD 會自動退回 RMS。" }
}

Step '4/4 database'
& $venvPy -c "from app.admin import db; p=db.default_db_path(); db.check_db_location(p); print('main', p, 'v', db.migrate(p)); q=db.identity_db_path(); print('identity', q, 'v', db.migrate_identity(q))"
if ($LASTEXITCODE -ne 0) { throw 'database migration failed' }

Write-Host @"

完成。尚未啟用任何新功能。
下一步：
  1) 先跑 benchmark：   .\scripts\bench_local.ps1 -Check ; .\scripts\bench_local.ps1
  2) 看結果決定設定，再用 .\scripts\start_backend.ps1 啟動（見 docs\LOCAL_SETUP.md）。
"@
