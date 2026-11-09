# Status-all: show Chat Shell + AstrBot + NapCat running status.
# Lists listening ports, related processes, and memory DB files.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

chcp 65001 | Out-Null

Write-Host '=== Listening Ports ===' -ForegroundColor Cyan
foreach ($p in @(8756, 6185, 6199, 6099)) {
    $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        foreach ($c in $conns) {
            Write-Host "  Port $p LISTENING PID=$($c.OwningProcess)" -ForegroundColor Green
        }
    } else {
        Write-Host "  Port $p not listening" -ForegroundColor Yellow
    }
}

Write-Host '' -ForegroundColor Cyan
Write-Host '=== Related Processes ===' -ForegroundColor Cyan
Get-Process | Where-Object { $_.ProcessName -in @('python', 'astrbot', 'QQ', 'NapCatWinBootMain') } |
    Select-Object Id, ProcessName, @{N='CPU(s)';E={$_.CPU}}, @{N='PM(MB)';E={[math]::Round($_.PM / 1MB, 1)}} |
    Format-Table -AutoSize

Write-Host '' -ForegroundColor Cyan
Write-Host '=== Keep Awake (v0.13.5) ===' -ForegroundColor Cyan
# 休眠会把 Shell / AstrBot / NapCat 一起冻结，期间 QQ 消息完全收不到
try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8756/v1/health' -TimeoutSec 5
    if ($h.keep_awake) {
        Write-Host '  keep_awake: true (service holds ES_SYSTEM_REQUIRED)' -ForegroundColor Green
    } else {
        Write-Host '  keep_awake: false - screen-off standby may freeze the bot!' -ForegroundColor Yellow
    }
} catch {
    Write-Host '  health endpoint unreachable (Shell not running?)' -ForegroundColor Yellow
}
$ka = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*keep-awake.ps1*' }
if ($ka) {
    Write-Host "  guard script: running (PID=$($ka[0].ProcessId))" -ForegroundColor Green
} else {
    Write-Host '  guard script: not running (run scripts\keep-awake.bat)' -ForegroundColor DarkGray
}

Write-Host '' -ForegroundColor Cyan
Write-Host '=== Memory DB ===' -ForegroundColor Cyan
$memDir = 'D:\projects\small_fat_fish\extra-brain'
if (Test-Path $memDir) {
    Get-ChildItem $memDir -Filter '*.db' | Select-Object Name, @{N='KB';E={[math]::Round($_.Length / 1KB)}} | Format-Table -AutoSize
} else {
    Write-Host '  Directory does not exist, memory DB not initialized.' -ForegroundColor Yellow
}

Write-Host '' -ForegroundColor Cyan
Write-Host '=== Quick Actions ===' -ForegroundColor Cyan
Write-Host '  Start all  : .\scripts\start-all.ps1 [QQ number]   (AstrBot + NapCat)'
Write-Host '  Start web  : python -m extra_brain.server --data-dir extra-brain   (port 8756)'
Write-Host '  Restart    : .\scripts\restart-all.ps1             (Shell + AstrBot + NapCat)'
Write-Host '  Stop all   : .\scripts\stop-all.ps1'
