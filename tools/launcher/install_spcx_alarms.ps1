<#
    install_spcx_alarms.ps1 -- start the SPCX alarms by themselves (2026-10-20)

    Registers a scheduled task, "SPCX alarms", that runs menu option A --
    open_candles.py day and night -- whenever you log on, and starts it
    again a minute after it stops for any reason other than Ctrl+C.
    Its window stays visible, so you can see it working; closing that
    window by accident brings it back within a minute.

    It first stops any SPCX alarms already running (an option A window),
    so there is only ever one copy, then restarts the merger watch so
    both pick up the heartbeat that lets each ring the phone if the
    other stops (heartbeat.py).

    Run once from the folder that holds open_candles.py, in an admin
    PowerShell. Running it again replaces the task with the same settings.
    Nothing here can trade.
#>

$ErrorActionPreference = 'Stop'
$folder = $PSScriptRoot
foreach ($need in 'open_candles.py', 'heartbeat.py', 'merger_watch.py') {
    if (-not (Test-Path (Join-Path $folder $need))) {
        Write-Host "STOP: $need is not in $folder - download it first. Nothing changed."
        exit 1
    }
}
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Host "STOP: 'python' was not found. Open a new PowerShell and try again."
    exit 1
}

# One copy only: stop SPCX alarms started from the menu (never a dry run).
$old = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'open_candles\.py' -and $_.CommandLine -match '--overnight' -and $_.CommandLine -notmatch '--dry-run' }
foreach ($p in $old) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
if ($old) { Write-Host "Stopped the SPCX alarms window that was running; the task takes over." ; Start-Sleep -Seconds 2 }

$args = '"' + (Join-Path $folder 'open_candles.py') + '" --from 04:00 --until 20:00 --rings-only --persist 30 --market-drop 1.25 --overnight'
$action = New-ScheduledTaskAction -Execute $python -Argument $args -WorkingDirectory $folder
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'SPCX alarms' -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description 'SPCX alarms day and night (open_candles.py). Read-only: alarms to the phone, never trades.' | Out-Null

# The merger watch reloads so it runs the version with the heartbeat.
if (Get-ScheduledTask -TaskName 'SPCX merger watch' -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName 'SPCX merger watch'
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName 'SPCX merger watch'
}
Start-ScheduledTask -TaskName 'SPCX alarms'
Start-Sleep -Seconds 5
foreach ($t in 'SPCX alarms', 'SPCX merger watch') {
    $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if ($task) { Write-Host "$t : $($task.State)" } else { Write-Host "$t : not installed" }
}
Write-Host "Both start by themselves at every logon, and come back within a minute if they stop."
