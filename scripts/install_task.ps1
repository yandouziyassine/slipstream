<#
.SYNOPSIS
  Registers, enables, disables or removes the Windows scheduled task that runs Slipstream's
  hourly paper-trading collector through WSL. Paper trading only; the task never places a real
  order or stores a password.

.DESCRIPTION
  -Install registers a task named "Slipstream hourly collector", DISABLED. It runs
  `wsl.exe -e bash /mnt/c/Code/slipstream/scripts/collect_hourly.sh` through
  `conhost.exe --headless`, so no console window opens (closing that window used to kill the
  run), once an hour at minute 05, as the current user, only while that user is logged on, with
  no stored password and standard (non-admin) privileges. A second instance is skipped rather
  than queued, and any run still going after 50 minutes is stopped. Re-running -Install keeps
  the task disabled.

  -Enable starts the hourly schedule. -Disable stops it without removing the task.
  -Uninstall removes the task. No switch touches any other scheduled task.

  This script never runs collect_hourly.sh itself.

.EXAMPLE
  powershell -File scripts/install_task.ps1 -Install

.EXAMPLE
  powershell -File scripts/install_task.ps1 -Enable
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Enable,
    [switch]$Disable,
    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName = "Slipstream hourly collector"
$ScriptPath = "/mnt/c/Code/slipstream/scripts/collect_hourly.sh"
$Conhost = Join-Path $env:SystemRoot "System32\conhost.exe"

$chosen = @($Install, $Enable, $Disable, $Uninstall) | Where-Object { $_ }
if (@($chosen).Count -ne 1) {
    Write-Host "Usage: install_task.ps1 -Install | -Enable | -Disable | -Uninstall (exactly one)"
    exit 1
}

function Get-CollectorTask {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

if ($Uninstall) {
    if ($null -eq (Get-CollectorTask)) {
        Write-Host "No scheduled task named '$TaskName' found; nothing to remove."
    }
    else {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    }
    exit 0
}

if ($Enable -or $Disable) {
    if ($null -eq (Get-CollectorTask)) {
        Write-Error "No scheduled task named '$TaskName'. Run -Install first."
        exit 1
    }
    if ($Enable) {
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Host "Enabled '$TaskName': it now runs hourly at :05."
    }
    else {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Host "Disabled '$TaskName': no further runs until -Enable."
    }
    exit 0
}

# -Install: run hourly at :05, starting from the next occurrence of that minute.
$now = Get-Date
$firstRun = Get-Date -Hour $now.Hour -Minute 5 -Second 0
if ($firstRun -le $now) {
    $firstRun = $firstRun.AddHours(1)
}

$action = New-ScheduledTaskAction -Execute $Conhost -Argument "--headless wsl.exe -e bash $ScriptPath"

$trigger = New-ScheduledTaskTrigger -Once -At $firstRun `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 50) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -Disable

# Interactive logon with no password, RunLevel Limited: standard (non-admin) privileges, and the
# task only runs while this user is logged on.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description ("Runs Slipstream's paper-trading hourly collector via WSL, with no window. " +
        "Paper trading only; no stored password; runs only while the user is logged on.") | Out-Null

Write-Host "Registered scheduled task '$TaskName' (DISABLED: nothing runs yet):"
Write-Host "  Hourly at :05 once enabled, as $env:USERDOMAIN\$env:USERNAME."
Write-Host "  Logged-on only, no stored password, standard (non-admin) privileges."
Write-Host "  Action: conhost.exe --headless wsl.exe -e bash $ScriptPath (no window)"
Write-Host "  MultipleInstances: IgnoreNew. Execution time limit: 50 minutes."
Write-Host ""
Write-Host "To start collecting:  powershell -File scripts/install_task.ps1 -Enable"
Write-Host "To stop collecting:   powershell -File scripts/install_task.ps1 -Disable"
Write-Host "To remove it:         powershell -File scripts/install_task.ps1 -Uninstall"
