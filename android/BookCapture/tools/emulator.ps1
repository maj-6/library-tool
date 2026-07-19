<#
.SYNOPSIS
Start (or stop) the Book Capture test emulator, bounded and detached.

.DESCRIPTION
Two things about this script are deliberate, both learned the hard way.

1. The emulator is launched DETACHED (Start-Process, own window handle, PID
   recorded to a file). It is a server: it never exits on its own. Launching it
   as a tracked child of a tool-runner means that runner waits forever and can
   keep file handles open long after the caller thinks it is done.

2. Waiting for boot is BOUNDED. If the device has not reported
   sys.boot_completed within -TimeoutSeconds, the emulator is killed, the tail
   of its log is printed, and the script exits non-zero. A hung emulator now
   fails loudly in minutes instead of spinning silently.

Also passes an explicit -dns-server. With a VPN adapter up (NordLynx et al.)
the emulator otherwise re-enumerates the tunnel's addresses in a loop -- the
log fills with "Ignore IPv6 address" forever and the guest never boots.

.EXAMPLE
pwsh -File tools/emulator.ps1 -Action start
pwsh -File tools/emulator.ps1 -Action stop
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start',
    [string]$Avd = 'whl_test',
    [int]$TimeoutSeconds = 420,
    [string]$DnsServer = '8.8.8.8',
    [switch]$Window
)

# Deliberately NOT 'Stop'. Windows PowerShell wraps a native command's stderr in
# an ErrorRecord, so under 'Stop' the very first `adb shell` poll -- which
# legitimately prints "no devices/emulators found" while the guest is still
# coming up -- becomes a terminating error and kills the wait loop.
$ErrorActionPreference = 'Continue'

$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { "$env:LOCALAPPDATA\Android\Sdk" }
$emulator = Join-Path $sdk 'emulator\emulator.exe'
$adb = Join-Path $sdk 'platform-tools\adb.exe'
$stateDir = Join-Path $env:TEMP 'whl-emulator'
$pidFile = Join-Path $stateDir "$Avd.pid"
$logFile = Join-Path $stateDir "$Avd.log"

New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

function Get-EmulatorProcess {
    if (-not (Test-Path $pidFile)) { return $null }
    $recorded = (Get-Content $pidFile -Raw).Trim()
    if (-not $recorded) { return $null }
    try { Get-Process -Id ([int]$recorded) -ErrorAction Stop } catch { $null }
}

function Stop-Emulator {
    $proc = Get-EmulatorProcess
    if ($proc) {
        Write-Host "stopping emulator (pid $($proc.Id))"
        try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop } catch {}
    }
    # emulator.exe is only a launcher: the VM is a separate qemu-system-x86_64*
    # process, and headless runs name it qemu-system-x86_64-headless. Match the
    # prefix or the real VM survives the "stop" and holds the AVD lock.
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -like 'qemu-system-x86_64*' } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    $avdDir = Join-Path $env:USERPROFILE ".android\avd\$Avd.avd"
    foreach ($lock in 'hardware-qemu.ini.lock', 'multiinstance.lock') {
        Remove-Item (Join-Path $avdDir $lock) -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($Action -eq 'stop') { Stop-Emulator; Write-Host 'stopped'; exit 0 }

if ($Action -eq 'status') {
    $proc = Get-EmulatorProcess
    if (-not $proc) { Write-Host 'not running'; exit 1 }
    $booted = (& $adb shell getprop sys.boot_completed 2>$null | Out-String).Trim()
    Write-Host "pid $($proc.Id); sys.boot_completed='$booted'"
    if ($booted -eq '1') { exit 0 } else { exit 1 }
}

if (Get-EmulatorProcess) { Write-Host 'already running; use -Action stop first'; exit 1 }
Stop-Emulator   # clear any stale locks left by a previous crash

$emulatorArgs = @(
    '-avd', $Avd,
    '-no-audio',
    '-no-boot-anim',
    '-no-snapshot',
    '-no-metrics',
    '-gpu', 'swiftshader_indirect',
    '-dns-server', $DnsServer
)
if (-not $Window) { $emulatorArgs += '-no-window' }

Write-Host "launching: emulator $($emulatorArgs -join ' ')"
$proc = Start-Process -FilePath $emulator -ArgumentList $emulatorArgs `
    -RedirectStandardOutput $logFile -RedirectStandardError "$logFile.err" `
    -WindowStyle Hidden -PassThru
$proc.Id | Set-Content $pidFile
Write-Host "pid $($proc.Id), log $logFile"

& $adb start-server | Out-Null
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    if ($proc.HasExited) {
        Write-Host "emulator exited early (code $($proc.ExitCode))"
        Get-Content $logFile -Tail 25 -ErrorAction SilentlyContinue
        exit 1
    }
    $booted = (& $adb shell getprop sys.boot_completed 2>$null | Out-String).Trim()
    if ($booted -eq '1') {
        & $adb shell input keyevent 82 2>$null | Out-Null   # dismiss the lock screen
        $elapsed = [int]($TimeoutSeconds - ($deadline - (Get-Date)).TotalSeconds)
        Write-Host "booted after ~${elapsed}s"
        exit 0
    }
    Start-Sleep -Seconds 5
}

Write-Host "TIMEOUT: no sys.boot_completed within ${TimeoutSeconds}s -- killing it"
Get-Content $logFile -Tail 25 -ErrorAction SilentlyContinue
Stop-Emulator
exit 1
