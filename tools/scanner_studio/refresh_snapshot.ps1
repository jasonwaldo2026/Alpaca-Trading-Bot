<#
    refresh_snapshot.ps1 -- reload Scanner Studio's saved copy (2026-10-16)

    The same thing as Settings -> Data Source -> Refresh, done from the
    computer itself so Scanner Studio accepts it (it refuses writes from
    any other device, by design). Run nightly by a scheduled task, so the
    screens that read the saved copy -- Outcomes, Signal Performance,
    sheets -- are never more than a day old.

    It only ASKS Scanner Studio to reload. Market Scanner's database is
    read, never written. Nothing here trades.

    Each run adds one line to refresh_snapshot.log beside this file.
#>

$log = Join-Path $PSScriptRoot 'refresh_snapshot.log'
try {
    $r = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8787/api/load' `
        -ContentType 'application/json' -Body '{"mode":"live"}' -TimeoutSec 30
    if ($r.ok) { $msg = 'reload started' }
    else       { $msg = "not started: $($r.error)" }
} catch {
    $msg = "FAILED: Scanner Studio did not answer ($($_.Exception.Message))"
}
Add-Content -Path $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
