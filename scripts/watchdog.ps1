# Watchdog: run start-silent.ps1 every N minutes forever (for Startup-folder use).
# The system blocks unprivileged scheduled tasks, so this plain loop is the keep-alive.
# Start-silent is idempotent: live services are skipped; only dead ones restart.
# Single-instance: exits immediately if another watchdog is already running.

$ErrorActionPreference = 'Continue'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $scriptDir 'start-silent.ps1'
$intervalMinutes = 5

# --- single-instance guard (matches our own command line via CIM) ---
$mine = $PID
try {
    $others = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.ProcessId -ne $mine -and $_.CommandLine -match 'watchdog\.ps1' }
    if ($others) {
        exit 0
    }
} catch {
    # CIM unavailable: proceed anyway (dup watchdogs are harmless, just noisy)
}

while ($true) {
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript | Out-Null
    } catch {
        # swallow: next round retries
    }
    Start-Sleep -Seconds ($intervalMinutes * 60)
}
