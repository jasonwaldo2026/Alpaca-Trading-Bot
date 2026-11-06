<#
    install_merger_watch.ps1 -- start merger_watch.py by itself (2026-10-18)

    Registers a scheduled task, "SPCX merger watch", that starts
    merger_watch.py --watch whenever you log on, and starts it again a
    minute after it stops for any reason. Run it once from the folder
    that holds merger_watch.py (C:\Dev\Feed-Check). Running it again
    replaces the task with the same settings.

    The watcher only reads public pages and sends Pushover messages.
    Nothing here can trade or bet.

    It runs while you are logged on to Windows. A PC that is asleep
    watches nothing, so set sleep to Never while plugged in.
#>

$ErrorActionPreference = 'Stop'
$folder = $PSScriptRoot
$script = Join-Path $folder 'merger_watch.py'
if (-not (Test-Path $script)) {
    Write-Host "STOP: merger_watch.py is not in $folder - put this file beside it."
    exit 1
}
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Host "STOP: 'python' was not found. Open a new PowerShell and try again."
    exit 1
}

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`" --watch" `
    -WorkingDirectory $folder
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName 'SPCX merger watch' -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description 'Read-only: rings the phone on Tesla-SpaceX merger filings, halts and odds.' | Out-Null
Start-ScheduledTask -TaskName 'SPCX merger watch'
Start-Sleep -Seconds 3
$state = (Get-ScheduledTask -TaskName 'SPCX merger watch').State
Write-Host "SPCX merger watch is installed and $state. It starts by itself at every logon."
