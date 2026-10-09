param([string]$OutputDirectory = (Split-Path $PSScriptRoot), [string]$SdkDirectory = '')
$ErrorActionPreference = 'Stop'
if (-not $SdkDirectory) {
    $cache = Join-Path (Split-Path $PSScriptRoot) '.downloads'
    New-Item -ItemType Directory -Force $cache | Out-Null
    $asset = (Get-Content (Join-Path $PSScriptRoot 'desktop-manifest.json') -Raw | ConvertFrom-Json).webview2
    $archive = Join-Path $cache 'webview2.zip'
    if (-not (Test-Path $archive)) { Invoke-WebRequest -UseBasicParsing $asset.url -OutFile $archive }
    $stream = [IO.File]::OpenRead($archive)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $digest = ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $hash.Dispose() }
    if ($digest -ne $asset.sha256) { throw 'WebView2 SDK checksum mismatch' }
    $SdkDirectory = Join-Path $cache ('webview2-' + $asset.version)
    if (-not (Test-Path $SdkDirectory)) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($archive, $SdkDirectory)
    }
}
New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
$compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$core = Join-Path $SdkDirectory 'lib\net462\Microsoft.Web.WebView2.Core.dll'
$forms = Join-Path $SdkDirectory 'lib\net462\Microsoft.Web.WebView2.WinForms.dll'
& $compiler /nologo /target:winexe /platform:x64 /optimize+ /utf8output /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Net.Http.dll /r:System.Web.Extensions.dll /r:Microsoft.CSharp.dll "/r:$core" "/r:$forms" "/out:$OutputDirectory\Breeze.exe" (Join-Path $PSScriptRoot 'Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Desktop compilation failed' }
Copy-Item $core,$forms -Destination $OutputDirectory -Force
Copy-Item (Join-Path $SdkDirectory 'runtimes\win-x64\native\WebView2Loader.dll') -Destination $OutputDirectory -Force
