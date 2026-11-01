# Install autostart + watchdog for AstrBot + NapCat (extra_brain companion bot).
# Strategy:
#   1) Try a Windows Scheduled Task (logon + 5-min watchdog).
#   2) If the OS policy blocks task registration (Access denied), fall back to the
#      current user's Startup folder with a windowless VBS launcher.
# The Startup fallback runs scripts\watchdog.ps1, which calls start-silent.ps1
# every 5 minutes forever; start-silent is idempotent (live services are skipped).
#
# Optionally persists DEEPSEEK_API_KEY to the User environment (never hardcoded here).
#
# Usage (normal user, no admin needed):
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 [-ApiKey "<key>"]

param(
    [string]$ApiKey
)

$ErrorActionPreference = 'Stop'

$taskName = 'ExtraBrainCompanionBot'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $scriptDir 'start-silent.ps1'
$watchScript = Join-Path $scriptDir 'watchdog.ps1'
$pwsh = (Get-Command powershell.exe).Source

# ---- API key persistence (User env; survives reboots, not committed) ----
if ($ApiKey) {
    [Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', $ApiKey, 'User')
    Write-Host '[install] DEEPSEEK_API_KEY persisted to User environment.' -ForegroundColor Green
} else {
    $existing = $env:DEEPSEEK_API_KEY
    if (-not $existing) { $existing = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User') }
    if ($existing) {
        Write-Host '[install] DEEPSEEK_API_KEY already available (process or User env), keeping.' -ForegroundColor Green
    } else {
        Write-Host '[install] WARNING: no DEEPSEEK_API_KEY found. Bot will start in read-only memory mode.' -ForegroundColor Yellow
    }
}

# ---- Attempt 1: scheduled task ----
$taskInstalled = $false
try {
    $action = New-ScheduledTaskAction -Execute $pwsh `
        -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $startScript)
    $triggerLogon  = New-ScheduledTaskTrigger -AtLogOn
    $triggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 9999)
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Days 0)
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $taskName -Action $action `
        -Trigger @($triggerLogon, $triggerRepeat) -Settings $settings `
        -Description 'extra_brain companion bot: start + keep-alive AstrBot & NapCat.' | Out-Null
    $taskInstalled = $true
    Write-Host "[install] Scheduled task '$taskName' registered (logon + 5-min watchdog)." -ForegroundColor Green
} catch {
    Write-Host "[install] Scheduled task unavailable ($($_.Exception.Message))." -ForegroundColor Yellow
}

# ---- Attempt 2 (fallback): Startup folder, windowless VBS ----
if (-not $taskInstalled) {
    $startup = [Environment]::GetFolderPath('Startup')
    $vbsPath = Join-Path $startup 'ExtraBrainCompanionBot.vbs'
    $vbs = @(
        'Set sh = CreateObject("WScript.Shell")'
        ('sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{0}""", 0, False' -f $watchScript)
    ) -join "`r`n"
    Set-Content -Path $vbsPath -Value $vbs -Encoding ASCII
    Write-Host "[install] Startup launcher installed: $vbsPath" -ForegroundColor Green
    Write-Host '[install] On every logon, watchdog.ps1 starts (5-min keep-alive, windowless).' -ForegroundColor Green
}

Write-Host '[install] Test keep-alive now: powershell -File scripts\start-silent.ps1' -ForegroundColor Cyan
Write-Host '[install] Remove:            powershell -File scripts\uninstall-autostart.ps1' -ForegroundColor Cyan
Write-Host '[install] Note: if QQ login expires (NapCat shows QR again), run visible start-all.ps1 once and scan.' -ForegroundColor Yellow
