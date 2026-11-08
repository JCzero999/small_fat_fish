# Stop-all: stop Chat Shell + AstrBot + NapCat/QQ.
# Idempotent: ports already free are reported and skipped.
#
# Bug fixed 2026-11-08: the old version assigned to `$pid`, which is a read-only
# PowerShell automatic variable. That assignment threw a terminating error and
# aborted the script before a single Stop-Process ran - so "stop-all" silently
# left AstrBot/NapCat alive, and start-all's port check then skipped the restart,
# leaving stale code running after a "restart".
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'

chcp 65001 | Out-Null

function Stop-ByPort {
    param([int]$Port, [string]$Name)
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Host "[$Name] No process found listening on port $Port, may already be stopped." -ForegroundColor Cyan
        return
    }
    foreach ($c in $conns) {
        $ownerPid = $c.OwningProcess
        Write-Host "[$Name] Found process listening on port $Port, PID=$ownerPid, stopping..." -ForegroundColor Yellow
        Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "[$Name] Stop failed, port $Port still in use." -ForegroundColor Red
    } else {
        Write-Host "[$Name] Stopped." -ForegroundColor Green
    }
}

# Chat Shell sidecar (web UI + /v1 Agent API)
Stop-ByPort -Port 8756 -Name 'Shell'

# AstrBot (web UI + OneBot reverse WS)
Stop-ByPort -Port 6185 -Name 'AstrBot'

# NapCat (injected into QQ): stop the listener, then clean up leftover QQ processes it spawned.
Stop-ByPort -Port 6099 -Name 'NapCat'

$targets = Get-Process | Where-Object { $_.ProcessName -in @('QQ', 'NapCatWinBootMain') } |
    Select-Object -ExpandProperty Id -Unique
foreach ($procId in $targets) {
    Write-Host "[NapCat] Ending leftover process PID=$procId ..." -ForegroundColor Yellow
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
}

Write-Host '' -ForegroundColor Cyan
Write-Host 'All stopped.' -ForegroundColor Cyan
