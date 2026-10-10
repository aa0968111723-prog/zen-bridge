#Requires -Version 5.1
# collect_env.ps1 — read-only environment capture for zbench (hardware.md §6.2).
# Changes NO system setting: no registry, power plan, BIOS, driver, Defender or TdrDelay.
# Any field that cannot be read is written as null; the script never fails because of one.
param([string]$Out = "$env:USERPROFILE\zen-bench\results\env.json", [int]$IdleSeconds = 30)
$ErrorActionPreference = 'Continue'
function Try-Get([scriptblock]$b) { try { & $b } catch { $null } }

$dir = Split-Path -Parent $Out
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }

$cpu  = Try-Get { Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed }
$dimm = Try-Get { Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed,ConfiguredClockSpeed,DeviceLocator,BankLabel,Manufacturer,PartNumber }
$gpu  = Try-Get { Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,DriverDate,AdapterRAM,VideoProcessor }
$os   = Try-Get { Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,TotalVisibleMemorySize,FreePhysicalMemory }
$pwr  = Try-Get { Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SystemInformation]::PowerStatus }
$plan = Try-Get { (powercfg /getactivescheme) -join ' ' }      # read-only query
$ev37 = Try-Get { (Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Kernel-Processor-Power';Id=37;StartTime=(Get-Date).AddHours(-24)} -ErrorAction SilentlyContinue | Measure-Object).Count }

# SMT sibling masks via GetLogicalProcessorInformationEx(RelationProcessorCore) — read-only.
$coreMasks = Try-Get {
  if (-not ('ZenTopo' -as [type])) {
    Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices; using System.Collections.Generic;
public static class ZenTopo {
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool GetLogicalProcessorInformationEx(int rel, IntPtr buf, ref uint len);
  public static List<ulong> CoreMasks() {
    uint len = 0; GetLogicalProcessorInformationEx(0, IntPtr.Zero, ref len);
    var r = new List<ulong>(); if (len == 0) return r;
    IntPtr p = Marshal.AllocHGlobal((int)len);
    try { if (!GetLogicalProcessorInformationEx(0, p, ref len)) return r;
      long off = 0;
      while (off < len) { IntPtr cur = new IntPtr(p.ToInt64() + off);
        int size = Marshal.ReadInt32(cur, 4); if (size <= 0) break;
        // SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX: Relationship(4) Size(4) then PROCESSOR_RELATIONSHIP:
        // Flags(1) EfficiencyClass(1) Reserved(20) GroupCount(2) GROUP_AFFINITY{Mask(8) Group(2)...} -> Mask at 8+24
        r.Add((ulong)Marshal.ReadInt64(cur, 8 + 24)); off += size; } }
    finally { Marshal.FreeHGlobal(p); }
    return r; } }
"@
  }
  [ZenTopo]::CoreMasks() | ForEach-Object { '0x{0:X}' -f $_ }
}

function Sample-Load([int]$sec) {
  $s = @()
  for ($i = 0; $i -lt $sec; $i++) {
    $p = Try-Get { Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'" }
    $q = Try-Get { Get-CimInstance Win32_PerfFormattedData_Counters_ProcessorInformation -Filter "Name='_Total'" }
    $m = Try-Get { Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory }
    $g = Try-Get { Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUAdapterMemory }
    $s += [pscustomobject]@{ t=(Get-Date).ToString('o'); cpu=$p.PercentProcessorTime;
      perf=$q.PercentProcessorPerformance; availMB=$m.AvailableMBytes; commit=$m.CommittedBytes;
      gpuShared=(Try-Get { ($g | Measure-Object SharedUsage -Sum).Sum }); gpuDedicated=(Try-Get { ($g | Measure-Object DedicatedUsage -Sum).Sum }) }
    Start-Sleep -Seconds 1 }
  $s }
$idle = Sample-Load $IdleSeconds
$top  = Try-Get { Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name,Id,CPU,WorkingSet64 }

function First-Line([string]$exe, [string[]]$a) {
  Try-Get { if (Get-Command $exe -ErrorAction SilentlyContinue) { ((& $exe @a 2>&1) | Select-Object -First 1) -join '' } }
}
$ollamaVer = Try-Get { (Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 3).version }
$ollamaPs  = Try-Get { Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/ps' -TimeoutSec 3 }

[ordered]@{
  collected_at = (Get-Date).ToString('o'); cpu = $cpu; core_masks = $coreMasks; dimms = $dimm; gpu = $gpu; os = $os
  power = @{ line = (Try-Get { $pwr.PowerLineStatus.ToString() }); battery_pct = (Try-Get { $pwr.BatteryLifePercent }); plan = $plan }
  kernel_power_event37_24h = $ev37; idle_samples = $idle; top_processes = $top
  vulkaninfo = (Try-Get { if (Get-Command vulkaninfo -ErrorAction SilentlyContinue) { (& vulkaninfo --summary 2>&1) -join "`n" } })
  ollama_cli = (First-Line 'ollama' @('--version')); ollama_version = $ollamaVer; ollama_ps = $ollamaPs
  thresholds = @{ bg_mean = 15; bg_spike = 30; bg_spike_frac = 0.25; throttle_drop = 0.15; throttle_s = 10 }
} | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $Out
Write-Host "wrote $Out"
