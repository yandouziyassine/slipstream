<#
.SYNOPSIS
  Registers or removes the Windows scheduled task that runs Slipstream's hourly paper-trading
  collector through WSL. Paper trading only; the task never places a real order or stores a
  password.

.DESCRIPTION
  -Install registers a task named "Slipstream hourly collector" that runs
  `wsl.exe -e bash /mnt/c/Code/slipstream/scripts/collect_hourly.sh` once an hour at minute 05,
  as the current user, only while that user is logged on, with no stored password and standard
  (non-admin) privileges. A second instance is skipped rather than queued, and any run still
  going after 50 minutes is stopped.

  -Uninstall removes that task. Neither switch touches any other scheduled task.

  This script only registers or removes the task; it never runs collect_hourly.sh itself.

.EXAMPLE
  powershell -File scripts/install_task.ps1 -Install

.EXAMPLE
  powershell -File scripts/install_task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName = "Slipstream hourly collector"
$ScriptPath = "/mnt/c/Code/slipstream/scripts/collect_hourly.sh"

if ($Install -and $Uninstall) {
    Write-Error "Specify only one of -Install or -Uninstall."
    exit 1
}
if (-not $Install -and -not $Uninstall) {
    Write-Host "Usage: install_task.ps1 -Install | -Uninstall"
    exit 1
}

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Host "No scheduled task named '$TaskName' found; nothing to remove."
    }
    else {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    }
    exit 0
}

# -Install: run hourly at :05, starting from the next occurrence of that minute.
$now = Get-Date
$firstRun = Get-Date -Hour $now.Hour -Minute 5 -Second 0
if ($firstRun -le $now) {
    $firstRun = $firstRun.AddHours(1)
}

$action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-e bash $ScriptPath"

$trigger = New-ScheduledTaskTrigger -Once -At $firstRun `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 50) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# Interactive logon with no password, RunLevel Limited: standard (non-admin) privileges, and the
# task only runs while this user is logged on.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description ("Runs Slipstream's paper-trading hourly collector via WSL. Paper trading " +
        "only; no stored password; runs only while the user is logged on.") | Out-Null

Write-Host "Registered scheduled task '$TaskName':"
Write-Host "  Runs hourly at :05 (first run $firstRun), as $env:USERDOMAIN\$env:USERNAME."
Write-Host "  Logged-on only, no stored password, standard (non-admin) privileges."
Write-Host "  Action: wsl.exe -e bash $ScriptPath"
Write-Host "  MultipleInstances: IgnoreNew. Execution time limit: 50 minutes."
Write-Host ""
Write-Host "To run it once now:   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove it:         powershell -File scripts/install_task.ps1 -Uninstall"
