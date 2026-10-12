param([ValidateSet('all','speech','evaluation','diarization')][string]$Profile = 'all', [string]$Python)
$ErrorActionPreference = 'Stop'
$TaskRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $TaskCandidates = @(
        (Join-Path $TaskRoot '.venv\Scripts\python.exe')
    )
    $TaskCandidates += @(Get-ChildItem -LiteralPath (Join-Path $TaskRoot '.python') -Filter python.exe -Recurse -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    $Python = $TaskCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $Python) { throw 'Install the Windows App first, or specify -Python with a Python 3.12 executable.' }
& $Python (Join-Path $PSScriptRoot 'setup_agent_suite.py') --profile $Profile
if ($LASTEXITCODE -ne 0) { throw 'Agent installation failed. Existing App configuration was preserved.' }
if ($Profile -in @('all','speech')) {
    & $Python (Join-Path $PSScriptRoot 'download_public_agent_models.py')
    if ($LASTEXITCODE -ne 0) { throw 'Public model setup failed; installed packages are preserved for retry.' }
}
& $Python (Join-Path $PSScriptRoot 'agent_suite_jobs.py') status
if ($LASTEXITCODE -ne 0) { throw 'Agent readiness check failed.' }
Write-Host 'Packages installed. Gated models require your own Hugging Face login and accepted model terms.'
