# Stop-all: stop AstrBot + NapCat + QQ.
# Run once to stop both processes with port-conflict idempotency protection.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'

chcp 65001 | Out-Null

# Stop AstrBot (port 6185)
$astrbotPort = 6185
$listening = Get-NetTCPConnection -LocalPort $astrbotPort -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    foreach ($c in $listening) {
        $pid = $c.OwningProcess
        Write-Host "[AstrBot] Found process listening on port $astrbotPort, PID=$pid, stopping..." -ForegroundColor Yellow
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    $still = Get-NetTCPConnection -LocalPort $astrbotPort -State Listen -ErrorAction SilentlyContinue
    if ($still) {
        Write-Host "[AstrBot] Stop failed, port $astrbotPort still in use." -ForegroundColor Red
    } else {
        Write-Host '[AstrBot] Stopped.' -ForegroundColor Green
    }
} else {
    Write-Host "[AstrBot] No process found listening on port $astrbotPort, may already be stopped." -ForegroundColor Cyan
}

# Stop NapCat + QQ (port 6099 + leftover QQ processes)
$napcatPort = 6099
$listening = Get-NetTCPConnection -LocalPort $napcatPort -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    foreach ($c in $listening) {
        $pid = $c.OwningProcess
        Write-Host "[NapCat] Found process listening on port $napcatPort, PID=$pid, stopping..." -ForegroundColor Yellow
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

$targets = Get-Process | Where-Object { $_.ProcessName -in @('QQ', 'NapCatWinBootMain') } | Select-Object -ExpandProperty Id -Unique
foreach ($p in $targets) {
    Write-Host "[NapCat] Ending leftover process PID=$p ..." -ForegroundColor Yellow
    Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
}

$still = Get-NetTCPConnection -LocalPort $napcatPort -State Listen -ErrorAction SilentlyContinue
if ($still) {
    Write-Host "[NapCat] Stop failed, port $napcatPort still in use." -ForegroundColor Red
} else {
    Write-Host '[NapCat] Stopped.' -ForegroundColor Green
}

Write-Host '' -ForegroundColor Cyan
Write-Host 'All stopped.' -ForegroundColor Cyan
