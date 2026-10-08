param(
    [switch]$CheckOnly,
    [string]$DesktopPath
)
$ErrorActionPreference = 'Stop'
# Use the Unicode Shell Link interface. WScript.Shell loses characters on an
# English Windows installation when a target contains a Chinese directory.

function Test-BreezeNonAsciiText([string]$Value) {
    if ([string]::IsNullOrEmpty($Value)) { return $false }
    foreach ($ch in $Value.ToCharArray()) {
        if ([int]$ch -gt 127) { return $true }
    }
    return $false
}

function Test-BreezeWritableDirectory([string]$Path) {
    try {
        $null = [System.IO.Directory]::CreateDirectory($Path)
        $probe = Join-Path $Path ([guid]::NewGuid().ToString('n') + '.tmp')
        [System.IO.File]::WriteAllText($probe, 'ok')
        try { [System.IO.File]::Delete($probe) } catch { }
        return $true
    } catch {
        return $false
    }
}

function Resolve-BreezeAsciiPath([string]$Root, [string]$Leaf) {
    if ([string]::IsNullOrEmpty($Root)) { return '' }
    if ($Root -match '^[A-Za-z]:$') { $Root = $Root + '\' }
    try {
        $candidate = [System.IO.Path]::GetFullPath((Join-Path $Root $Leaf))
    } catch {
        return ''
    }
    if (Test-BreezeNonAsciiText $candidate) { return '' }
    return $candidate
}

function Get-BreezeTopmostMissingAncestor([string]$Path) {
    # Top-most path component that does not exist yet. Empty when the whole path exists.
    # Never returns a drive root; that directory was not created by this script.
    if ([string]::IsNullOrEmpty($Path)) { return '' }
    $full = [System.IO.Path]::GetFullPath($Path)
    $current = $full
    $topMissing = ''
    while (-not [string]::IsNullOrEmpty($current) -and -not [System.IO.Directory]::Exists($current)) {
        $root = [System.IO.Path]::GetPathRoot($current)
        if ([string]::IsNullOrEmpty($root)) { break }
        if ($current.TrimEnd('\') -eq $root.TrimEnd('\')) { break }
        $topMissing = $current
        $parent = [System.IO.Path]::GetDirectoryName($current)
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $current) { break }
        $current = $parent
    }
    return $topMissing
}

function Remove-BreezeCreatedDirectory([string]$Path) {
    # Best-effort. Cleanup must not fail shortcut creation, and must not remove a drive root.
    if ([string]::IsNullOrEmpty($Path)) { return }
    try {
        $full = [System.IO.Path]::GetFullPath($Path)
        $root = [System.IO.Path]::GetPathRoot($full)
        if ([string]::IsNullOrEmpty($root) -or $full.TrimEnd('\') -eq $root.TrimEnd('\')) { return }
        if ([System.IO.Directory]::Exists($full)) {
            [System.IO.Directory]::Delete($full, $true)
        }
    } catch { }
}

function Get-BreezeAsciiTemp {
    # csc.exe (Windows PowerShell 5.1) fails when TEMP is not pure ASCII.
    foreach ($candidate in @(
        (Resolve-BreezeAsciiPath $env:ProgramData 'BreezeLiveRoom\tmp'),
        (Resolve-BreezeAsciiPath $env:PUBLIC 'BreezeLiveRoom\tmp'),
        (Resolve-BreezeAsciiPath $env:SystemDrive 'BreezeLiveRoomTmp')
    )) {
        if ([string]::IsNullOrEmpty($candidate)) { continue }
        $missingRoot = Get-BreezeTopmostMissingAncestor $candidate
        if (Test-BreezeWritableDirectory $candidate) {
            $script:BreezeCreatedTempRoot = $missingRoot
            return $candidate
        }
        Remove-BreezeCreatedDirectory $missingRoot
    }
    throw 'No ASCII-only writable directory is available for shortcut compilation.'
}

$breezeOriginalTemp = $env:TEMP
$breezeOriginalTmp = $env:TMP
$script:BreezeCreatedTempRoot = $null
try {
    if ((Test-BreezeNonAsciiText $env:TEMP) -or (Test-BreezeNonAsciiText $env:TMP)) {
        $breezeAsciiTemp = Get-BreezeAsciiTemp
        Write-Host ('Using ASCII TEMP for shortcut helper: ' + $breezeAsciiTemp)
        $env:TEMP = $breezeAsciiTemp
        $env:TMP = $breezeAsciiTemp
    }
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
class BreezeShellLink { }

[ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IBreezeShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, IntPtr data, uint flags);
    void GetIDList(out IntPtr list);
    void SetIDList(IntPtr list);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int size);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int size);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string text);
    void GetHotkey(out short hotkey);
    void SetHotkey(short hotkey);
    void GetShowCmd(out int command);
    void SetShowCmd(int command);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, out int index);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
    void Resolve(IntPtr window, uint flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
}

public static class BreezeDesktopShortcut {
    public static void Create(string target, string directory, string link) {
        IBreezeShellLinkW shell = (IBreezeShellLinkW)new BreezeShellLink();
        try {
            shell.SetPath(target);
            shell.SetWorkingDirectory(directory);
            shell.SetDescription("Breeze Live Room");
            shell.SetShowCmd(1);
            ((IPersistFile)shell).Save(link, true);
        } finally { Marshal.FinalReleaseComObject(shell); }
    }
    public static void Verify(string target, string directory, string link) {
        IBreezeShellLinkW shell = (IBreezeShellLinkW)new BreezeShellLink();
        try {
            ((IPersistFile)shell).Load(link, 0);
            StringBuilder savedTarget = new StringBuilder(32768);
            StringBuilder savedDirectory = new StringBuilder(32768);
            shell.GetPath(savedTarget, savedTarget.Capacity, IntPtr.Zero, 4);
            shell.GetWorkingDirectory(savedDirectory, savedDirectory.Capacity);
            if (!String.Equals(savedTarget.ToString(), target, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(savedDirectory.ToString(), directory, StringComparison.OrdinalIgnoreCase)) {
                throw new InvalidOperationException("The Unicode shortcut target or directory was not preserved.");
            }
        } finally { Marshal.FinalReleaseComObject(shell); }
    }
}
'@
} finally {
    $env:TEMP = $breezeOriginalTemp
    $env:TMP = $breezeOriginalTmp
    Remove-BreezeCreatedDirectory $script:BreezeCreatedTempRoot
}

$WorkingDirectory = $PSScriptRoot
if ([string]::IsNullOrEmpty($DesktopPath)) {
    $desktop = [Environment]::GetFolderPath('Desktop')
} else {
    $desktop = $DesktopPath
}
if (-not $desktop) { exit 1 }
[System.IO.Directory]::CreateDirectory($desktop) | Out-Null
foreach ($entry in @(@('Breeze Live Room', 'start.bat'), @('Breeze Update', 'update.bat'), @('Breeze Doctor', 'doctor.bat'))) {
    $shortcutPath = Join-Path $desktop ($entry[0] + '.lnk')
    $target = Join-Path $WorkingDirectory $entry[1]
    if (-not $CheckOnly) {
        [BreezeDesktopShortcut]::Create($target, $WorkingDirectory, $shortcutPath)
    } elseif (-not (Test-Path -LiteralPath $shortcutPath)) {
        Write-Output ("Shortcut not found: " + $shortcutPath)
        exit 2
    }
    try {
        [BreezeDesktopShortcut]::Verify($target, $WorkingDirectory, $shortcutPath)
    } catch {
        Write-Output ('Shortcut target or directory is incorrect: ' + $shortcutPath + ' (' + $_.Exception.Message + ')')
        exit 1
    }
}
