#Requires -Version 5.1
<#
.SYNOPSIS
  Start the live room with the local backend flags, and/or the admin backend (127.0.0.1:8791).
.EXAMPLE
  .\scripts\start_backend.ps1 -Admin -OpenAdmin          # admin only, opens the one-time login URL
  .\scripts\start_backend.ps1 -Live -Local -Ledger -TM    # live room with local translation + ledger + TM
  .\scripts\start_backend.ps1 -Live -Local -Ledger -TM -Vad -Admin
.NOTES
  Flags only set environment variables for the processes started here; nothing is written to
  the registry or the user environment. The admin backend never binds anything but loopback
  and refuses port 8645.
#>
param(
  [switch]$Live, [switch]$Admin, [switch]$OpenAdmin,
  [switch]$Local, [switch]$Ledger, [switch]$TM, [switch]$Vad, [switch]$NoEmbed,
  [string]$Model = "qwen3:4b",
  [int]$Threads = 4,
  [int]$QueueMax = 4,
  [double]$StaleSeconds = 8,
  [ValidateSet('skip', 'merge', 'off')][string]$StalePolicy = 'skip',
  [ValidateSet('idle', 'below_normal', 'normal')][string]$TranslatePriority = 'below_normal',
  [int]$AdminPort = 8791
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw '找不到 .venv。請先執行 scripts\setup_local.ps1。' }
if ($AdminPort -eq 8645) { throw '8645 保留給 Hermes，不能使用。' }
if (-not ($Live -or $Admin)) { $Live = $true }

$env:PYTHONUTF8 = '1'
# --- translation (hardware.md §7 defaults; every value overridable) ---
if ($Local) {
  $env:BREEZE_TRANSLATE_ENGINE = 'local'
  $env:BREEZE_TRANSLATE_BASE_URL = 'http://127.0.0.1:11434/v1'
  $env:BREEZE_TRANSLATE_MODEL = $Model
  $env:BREEZE_TRANSLATE_NUM_THREAD = "$Threads"
  $env:BREEZE_TRANSLATE_NUM_CTX = '2048'
  $env:BREEZE_TRANSLATE_KEEP_ALIVE = '-1'
}
$env:BREEZE_TRANSLATE_QUEUE = "$QueueMax"
$env:BREEZE_TRANSLATE_STALE_S = "$StaleSeconds"
$env:BREEZE_TRANSLATE_STALE_POLICY = $StalePolicy
$env:BREEZE_TRANSLATE_PRIORITY = $TranslatePriority
$env:ZEN_LEDGER = $(if ($Ledger) { '1' } else { '0' })
$env:BREEZE_TM = $(if ($TM) { '1' } else { '0' })
$env:BREEZE_VAD = $(if ($Vad) { 'silero' } else { 'off' })
$env:ZEN_EMBED = $(if ($NoEmbed) { '0' } else { '1' })
$env:ZEN_ADMIN_PORT = "$AdminPort"

if ($Admin) {
  if ($OpenAdmin) { $env:ZEN_ADMIN_OPEN_BROWSER = '1' }
  Write-Host "啟動後台 http://127.0.0.1:$AdminPort/admin （新視窗）"
  Start-Process -FilePath $py -ArgumentList '-m', 'app.admin.run' -WorkingDirectory $root
}
if ($Live) {
  Write-Host ("啟動直播服務：engine={0} model={1} threads={2} queue={3} stale={4}s/{5} ledger={6} tm={7} vad={8}" -f `
    $env:BREEZE_TRANSLATE_ENGINE, $Model, $Threads, $QueueMax, $StaleSeconds, $StalePolicy, $env:ZEN_LEDGER, $env:BREEZE_TM, $env:BREEZE_VAD)
  $env:BREEZE_OPEN_BROWSER = '1'
  & $py -m app.run
}
