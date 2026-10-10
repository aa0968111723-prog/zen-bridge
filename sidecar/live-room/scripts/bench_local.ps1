#Requires -Version 5.1
<#
.SYNOPSIS
  Run the zen-bridge local benchmark BEFORE enabling any local-backend feature (hardware.md §6).
.DESCRIPTION
  Read-only: changes no system setting. Uses its own venv in %USERPROFILE%\zen-bench\venv
  (never the App's venv). Downloads nothing except psutil/numpy from PyPI into that venv,
  and only after you type yes. Ollama is only queried over HTTP. Results stay on this PC.
.EXAMPLE
  .\scripts\bench_local.ps1 -Check
  .\scripts\bench_local.ps1                       # layers asr,llm,embed, 3 reps
  .\scripts\bench_local.ps1 -Layers llm,embed -Reps 1 -Cooldown 5
  .\scripts\bench_local.ps1 -Layers concurrent     # layer 3 (10 min per rep)
#>
param(
  [string]$Root = "$env:USERPROFILE\zen-bench",
  [string]$Layers = "asr,llm,embed",
  [int]$Reps = 3,
  [double]$Cooldown = 30,
  [switch]$AllowBattery,
  [switch]$Check,
  [string]$Python = ""
)
$ErrorActionPreference = 'Stop'
if (-not $Check) {
  throw 'Benchmark 暫停：新版 QA 指出 7 個 P0；請先接收修正版 patch，再做效能測試。-Check 只執行預檢。'
}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bench = Join-Path $here 'bench'
New-Item -ItemType Directory -Force -Path $Root, "$Root\results", "$Root\audio" | Out-Null
if (-not (Test-Path "$Root\matrix.json")) {
  Copy-Item "$bench\matrix.example.json" "$Root\matrix.json"
  Write-Host "已建立 $Root\matrix.json（範本）。請先修改裡面的路徑，再重新執行。" -ForegroundColor Yellow
}
$venv = Join-Path $Root 'venv'
$py = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $py)) {
  if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $Python = 'py' } else { $Python = 'python' }
  }
  $ans = Read-Host "要在 $venv 建立 benchmark 專用 venv 並從 PyPI 安裝 psutil、numpy 嗎？輸入 yes 繼續"
  if ($ans -ne 'yes') { Write-Host '已取消。'; exit 1 }
  if ($Python -eq 'py') { & py -3 -m venv $venv } else { & $Python -m venv $venv }
  & $py -m pip install --disable-pip-version-check -q psutil numpy
}
$env:ZBENCH_ROOT = $Root
$zargs = @("$bench\zbench.py", '--matrix', "$Root\matrix.json", '--layers', $Layers, '--reps', $Reps, '--cooldown', $Cooldown)
if ($AllowBattery) { $zargs += '--allow-battery' }
if ($Check) { $zargs += '--check' }
& $py @zargs
exit $LASTEXITCODE
