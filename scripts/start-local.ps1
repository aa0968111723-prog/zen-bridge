<#
  禪譯（龜龜／小華）完全本機啟動 — 不需要任何雲端帳號或網路。
  - 資料：%LOCALAPPDATA%\ZenBridge\zen-ui.sqlite3 與 audio\（可用 -DataDir 或 ZEN_UI_DATA_DIR 改）
  - 只綁 127.0.0.1（-Lan 才會開放區域網路）；預設連接埠 8785（絕不使用 8645）
  - Breeze 現場／管理後台：預設連 http://127.0.0.1:8780 與 http://127.0.0.1:8791（BREEZE_LIVE_URL / BREEZE_ADMIN_URL）
  用法：powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1 [-Port 8785] [-DataDir D:\ZenData] [-Lan] [-Rebuild] [-NoBrowser]
#>
param(
  [int]$Port = 8785,
  [string]$DataDir = "",
  [switch]$Lan,
  [switch]$Rebuild,
  [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if ($Port -in 8645, 8780, 8791) { throw "連接埠 $Port 保留給其他程式，請換一個 -Port。" }

# Node 22.13+（內建 node:sqlite，不需要原生模組）。安裝版可把 node.exe 放在 runtime\node\ 或設定 ZEN_NODE。
$node = $env:ZEN_NODE
if (-not $node -and (Test-Path "$root\runtime\node\node.exe")) { $node = "$root\runtime\node\node.exe" }
if (-not $node) { $node = (Get-Command node -ErrorAction SilentlyContinue).Source }
if (-not $node) { throw '找不到 Node.js 22.13 以上版本（ZEN_NODE、runtime\node\node.exe 或 PATH）。' }
$ver = [version]((& $node --version).TrimStart('v'))
if ($ver -lt [version]'22.13.0') { throw "Node 版本 $ver 太舊，需要 22.13 以上。" }

# 清掉雲端設定，確保走本機模式。
foreach ($name in 'DATABASE_URL','AUDIO_DIR','CLOUDFLARE_API_TOKEN') { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
$env:DEPLOY_TARGET = 'local'
$env:ZEN_UI_PORT = "$Port"
$env:ZEN_UI_HOST = $(if ($Lan) { '0.0.0.0' } else { '127.0.0.1' })
if ($DataDir) { $env:ZEN_UI_DATA_DIR = $DataDir }
if (-not $env:BREEZE_LIVE_URL) { $env:BREEZE_LIVE_URL = 'http://127.0.0.1:8780' }
if (-not $env:BREEZE_ADMIN_URL) { $env:BREEZE_ADMIN_URL = 'http://127.0.0.1:8791' }

if ($Rebuild -or -not (Test-Path "$root\dist\server")) {
  if (-not (Test-Path "$root\node_modules")) { throw '尚未安裝相依套件：請先在有網路時執行一次 pnpm install（之後可離線）。' }
  Write-Host '建置禪譯（pnpm build）…'
  $pnpm = (Get-Command pnpm -ErrorAction SilentlyContinue).Source
  if ($pnpm) { & $pnpm build } else { & $node --experimental-strip-types scripts/run-framework.mjs build }
  if ($LASTEXITCODE -ne 0) { throw "建置失敗（exit $LASTEXITCODE）。" }
}

$url = "http://127.0.0.1:$Port/"
if (-not $NoBrowser) {
  Start-Job -ScriptBlock {
    param($u)
    for ($i = 0; $i -lt 60; $i++) {
      try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 $u | Out-Null; Start-Process $u; break } catch { Start-Sleep -Milliseconds 500 }
    }
  } -ArgumentList $url | Out-Null
}
Write-Host "禪譯本機版：$url（Ctrl+C 結束）"
& $node --experimental-strip-types --disable-warning=ExperimentalWarning scripts/start.mjs --local
exit $LASTEXITCODE
