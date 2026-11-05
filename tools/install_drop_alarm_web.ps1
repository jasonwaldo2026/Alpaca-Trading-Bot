# install_drop_alarm_web.ps1 - set up the phone Drop Alarm page once (5 Nov 2026).
#
# Run in an ADMINISTRATOR PowerShell, from C:\Dev\Feed-Check:
#     powershell -ExecutionPolicy Bypass -File .\install_drop_alarm_web.ps1
#
# It does three things and prints the phone link at the end:
#   1. a Windows Firewall rule: port 8790, Tailscale devices (100.64.0.0/10) only
#   2. a scheduled task "SPCX drop alarm page": starts drop_alarm_web.py one
#      minute after you log on, with no window, and restarts it if it stops
#   3. starts it now
# Running it again is safe: it replaces its own rule and task.

$ErrorActionPreference = 'Stop'
$Folder = $PSScriptRoot
$Port = 8790
$Task = 'SPCX drop alarm page'
$Rule = 'Drop Alarm (Tailscale only)'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Please run this in an Administrator PowerShell.' -ForegroundColor Red; exit 1
}
if (-not (Test-Path (Join-Path $Folder 'drop_alarm_web.py'))) {
    Write-Host "drop_alarm_web.py is not in $Folder - download it there first." -ForegroundColor Red; exit 1
}

# 1. Firewall: only Tailscale devices may reach the page.
Get-NetFirewallRule -DisplayName $Rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $Rule -Direction Inbound -Protocol TCP -LocalPort $Port `
    -RemoteAddress 100.64.0.0/10 -Action Allow -Profile Any | Out-Null
Write-Host "  [ OK ]  firewall: port $Port, Tailscale devices only" -ForegroundColor Green

# 2. The logon task, windowless (pythonw) when available.
$py = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python -ErrorAction Stop).Source }
$action = New-ScheduledTaskAction -Execute $py -Argument 'drop_alarm_web.py' -WorkingDirectory $Folder
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT1M'
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Drop Alarm page for the phone (drop_alarm_web.py), port 8790.' | Out-Null
Write-Host "  [ OK ]  task '$Task' starts it at every logon ($py)" -ForegroundColor Green

# 3. Start it now, then check it answers.
Start-ScheduledTask -TaskName $Task
Start-Sleep -Seconds 4
$up = $false
try { $up = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$Port/").StatusCode -gt 0 } catch {
    $up = [bool]$_.Exception.Response   # 403 without the code still means it is running
}
if ($up) { Write-Host '  [ OK ]  the page is running' -ForegroundColor Green }
else { Write-Host '  [ !! ]  the page did not answer - see drop_alarm_web.log in this folder' -ForegroundColor Red }

Write-Host ''
Write-Host '  Phone link (keep it private - it holds your access code):' -ForegroundColor Cyan
Push-Location $Folder; & (Get-Command python).Source drop_alarm_web.py --link; Pop-Location
Write-Host '  Open it in Safari on the phone, then Share -> Add to Home Screen.'
