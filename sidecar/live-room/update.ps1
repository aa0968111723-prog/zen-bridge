param([switch]$Rollback, [switch]$Check)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$asset = (Get-Content bootstrap-manifest.json -Raw | ConvertFrom-Json).python
$python = Join-Path $PSScriptRoot ('.python\' + $asset.version + '\tools\python.exe')
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host 'Run install.bat to prepare the private runtime first.'
    exit 1
}
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path $PSScriptRoot 'tmp'
$env:TMP = $env:TEMP
$env:SSLKEYLOGFILE = $null
$arguments = @('scripts\update.py')
if ($Rollback) { $arguments += '--rollback' }
if ($Check) { $arguments += '--check' }
& $python @arguments
exit $LASTEXITCODE
