# Remove autostart for the extra_brain companion bot:
#   - scheduled task ExtraBrainCompanionBot (if present)
#   - Startup-folder launcher ExtraBrainCompanionBot.vbs (if present)
#   - running watchdog loops (powershell.exe ... watchdog.ps1)
# Running AstrBot/NapCat processes are untouched; use stop-all.ps1 for those.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\uninstall-autostart.ps1

$ErrorActionPreference = 'Continue'
$taskName = 'ExtraBrainCompanionBot'

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-Host "[uninstall] FAILED to remove task $taskName." -ForegroundColor Red
} else {
    Write-Host "[uninstall] Scheduled task '$taskName' removed (or never existed)." -ForegroundColor Green
}

$vbsPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'ExtraBrainCompanionBot.vbs'
if (Test-Path $vbsPath) {
    Remove-Item $vbsPath -Force
    Write-Host "[uninstall] Startup launcher removed: $vbsPath" -ForegroundColor Green
} else {
    Write-Host '[uninstall] No Startup launcher found.' -ForegroundColor Green
}

try {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -match 'watchdog\.ps1' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host '[uninstall] Watchdog loops stopped (if any).' -ForegroundColor Green
} catch {
    Write-Host '[uninstall] Could not enumerate watchdog processes (manual check if needed).' -ForegroundColor Yellow
}
