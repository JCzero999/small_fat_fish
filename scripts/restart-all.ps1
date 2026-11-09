# Restart-all: stop AstrBot + NapCat/QQ + Chat Shell, then start them again.
# Order matters: stop first (ports must be free), then shell (8756), then AstrBot (6185/6199), then NapCat (6099).
# Why a separate script: start-all.ps1 only covers AstrBot + NapCat; the Chat Shell sidecar (8756) was
# historically started by hand, so a "full restart" used to forget it and keep serving stale code.
#
# Usage:
#   .\scripts\restart-all.ps1            # quick login 1178469499
#   .\scripts\restart-all.ps1 123456789  # another QQ number
#   .\scripts\restart-all.ps1 -SkipQQ    # web only (shell + AstrBot, keep QQ/NapCat untouched)
#
# Note: code changes in src/extra_brain or the AstrBot plugin ONLY take effect after this restart.

param(
    [string]$QQNumber = '1178469499',
    [switch]$SkipQQ,
    [switch]$NoConfigSync,
    [switch]$NoKeepAwake
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'
chcp 65001 | Out-Null

$repoRoot      = Split-Path -Parent $PSScriptRoot
$runtimeRoot   = 'D:\projects_software\astrbot-runtime'
$shellPort     = 8756
$py            = 'C:\Users\19758\miniconda3\python.exe'
$repoConfig    = Join-Path $repoRoot 'config\user_config.json'
$runtimeConfig = Join-Path $runtimeRoot 'config\user_config.json'

function Get-NormalizedJson {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return '' }
    try {
        $obj = Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        return ($obj | ConvertTo-Json -Depth 20 -Compress)
    } catch { return '' }
}

# The AstrBot plugin resolves config/user_config.json and persona/xiaotun_v1.md relative to
# its own working directory (astrbot-runtime), NOT the repo. Until 2026-11-08 those runtime
# copies silently drifted: tone / owner_accounts edits and the v0.13.4 persona never reached
# QQ, so the web side and QQ side behaved differently while both claimed the same persona.
function Sync-RuntimeAssets {
    $assets = @(
        @{ Label = 'config'; Src = $repoConfig; Dst = $runtimeConfig; IsJson = $true },
        @{ Label = 'persona'; Src = (Join-Path $repoRoot 'persona\xiaotun_v1.md'); Dst = (Join-Path $runtimeRoot 'persona\xiaotun_v1.md'); IsJson = $false }
    )
    foreach ($a in $assets) {
        if (-not (Test-Path $a.Src)) {
            Write-Host "[$($a.Label)] repo copy missing: $($a.Src)" -ForegroundColor Yellow
            continue
        }
        if ($a.IsJson) {
            $src = Get-NormalizedJson -Path $a.Src
            $dst = Get-NormalizedJson -Path $a.Dst
            $same = ($src -and $src -eq $dst)
        } else {
            $srcHash = (Get-FileHash $a.Src -Algorithm SHA256).Hash
            $dstHash = ''
            if (Test-Path $a.Dst) { $dstHash = (Get-FileHash $a.Dst -Algorithm SHA256).Hash }
            $same = ($srcHash -eq $dstHash)
        }
        if ($same) {
            Write-Host "[$($a.Label)] runtime copy in sync with repo." -ForegroundColor Green
            continue
        }
        if (Test-Path $a.Dst) {
            $backup = $a.Dst + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
            Copy-Item $a.Dst $backup -Force
            Write-Host "[$($a.Label)] backup -> $($backup | Split-Path -Leaf)" -ForegroundColor DarkGray
        }
        $parent = Split-Path -Parent $a.Dst
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
        Copy-Item $a.Src $a.Dst -Force
        Write-Host "[$($a.Label)] synced repo -> astrbot-runtime (plugin reads the runtime copy)." -ForegroundColor Green
    }
}


function Wait-PortListening {
    param([int]$Port, [int]$TimeoutSeconds = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

# 2026-11-09：笔记本息屏 10 分钟后 Windows 进入「新型待机」，三个进程被整体冻结
# （实测 16:17 因 Idle Timeout、18:18 因合盖，各冻 56 分钟），期间 QQ 消息全收不到。
# powercfg 的「睡眠超时=0」挡不住由息屏触发的 Modern Standby，必须用电源请求锁住系统。
function Start-KeepAwake {
    param([switch]$Skip)
    if ($Skip) {
        Write-Host '[keep-awake] skipped (-NoKeepAwake).' -ForegroundColor DarkGray
        return
    }
    $running = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*keep-awake.ps1*' }
    if ($running) {
        Write-Host "[keep-awake] already running (PID=$($running[0].ProcessId))." -ForegroundColor Green
        return
    }
    $script = Join-Path $PSScriptRoot 'keep-awake.ps1'
    if (-not (Test-Path $script)) {
        Write-Host "[keep-awake] script missing: $script" -ForegroundColor Yellow
        return
    }
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', $script) `
        -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 1
    $ok = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*keep-awake.ps1*' }
    if ($ok) {
        Write-Host "[keep-awake] started (PID=$($ok[0].ProcessId)) - system stays awake while screen may sleep." -ForegroundColor Green
    } else {
        Write-Host '[keep-awake] WARNING: not running; QQ may go offline when the screen turns off.' -ForegroundColor Red
    }
}

function Stop-ByPort {
    param([int]$Port, [string]$Name)
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Host "[$Name] not running (port $Port free)." -ForegroundColor DarkGray
        return
    }
    foreach ($c in $conns) {
        Write-Host "[$Name] stopping PID=$($c.OwningProcess) (port $Port) ..." -ForegroundColor Yellow
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "[$Name] WARNING: port $Port still in use." -ForegroundColor Red
    } else {
        Write-Host "[$Name] stopped." -ForegroundColor Green
    }
}

Write-Host '=== 1/3 Stop ===' -ForegroundColor Cyan

# Chat Shell sidecar (web UI + /v1 Agent API)
Stop-ByPort -Port $shellPort -Name 'Shell'

if ($SkipQQ) {
    # Web-only restart: AstrBot must go too (plugin code lives in the runtime), QQ/NapCat stay up.
    Stop-ByPort -Port 6185 -Name 'AstrBot'
} else {
    & (Join-Path $PSScriptRoot 'stop-all.ps1')
}

Write-Host ''
Write-Host '=== 2/3 Start Shell (web) ===' -ForegroundColor Cyan

if (-not $NoConfigSync) { Sync-RuntimeAssets }

Start-KeepAwake -Skip:$NoKeepAwake

if (Test-Path $py) {
    $shellCmd = '/c chcp 65001 >nul && title Chat Shell :8756 && "' + $py + '" -m extra_brain.server --data-dir extra-brain'
    Start-Process -FilePath 'cmd.exe' -ArgumentList $shellCmd -WorkingDirectory $repoRoot -WindowStyle Normal | Out-Null
    if (Wait-PortListening -Port $shellPort -TimeoutSeconds 40) {
        Write-Host "[Shell] started, port $shellPort listening." -ForegroundColor Green
    } else {
        Write-Host "[Shell] WARNING: port $shellPort not listening after 40s - check the 'Chat Shell :8756' window." -ForegroundColor Red
    }
} else {
    Write-Host "[Shell] ERROR: python not found: $py" -ForegroundColor Red
}

Write-Host ''
Write-Host '=== 3/3 Start AstrBot + NapCat (QQ) ===' -ForegroundColor Cyan

if ($SkipQQ) {
    if (Test-Path (Join-Path $PSScriptRoot 'start-all.ps1')) {
        # start-all is idempotent: AstrBot is down (we stopped it), NapCat is up and will be skipped.
        & (Join-Path $PSScriptRoot 'start-all.ps1') $QQNumber
    }
} else {
    & (Join-Path $PSScriptRoot 'start-all.ps1') $QQNumber
}

Write-Host ''
Write-Host '=== Summary ===' -ForegroundColor Cyan
foreach ($p in @($shellPort, 6185, 6199, 6099)) {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($c) { Write-Host "  Port $p LISTENING (PID $($c[0].OwningProcess))" -ForegroundColor Green }
    else    { Write-Host "  Port $p not listening" -ForegroundColor Yellow }
}

$cfg = Join-Path $repoRoot 'config\user_config.json'
$token = ''
if (Test-Path $cfg) {
    try {
        $token = (Get-Content $cfg -Raw -Encoding UTF8 | ConvertFrom-Json).shell.token
    } catch { $token = '' }
}

Write-Host ''
Write-Host "  Web chat : http://127.0.0.1:$shellPort/" -ForegroundColor Green
if ($token) { Write-Host "  Shell token (also printed in the Chat Shell window): $token" -ForegroundColor Green }
Write-Host "  AstrBot  : http://127.0.0.1:6185/   NapCat WebUI: http://127.0.0.1:6099/webui" -ForegroundColor Green
Write-Host '  Browser: press Ctrl+F5 after a restart, otherwise the page keeps the old token and old JS.' -ForegroundColor Yellow
