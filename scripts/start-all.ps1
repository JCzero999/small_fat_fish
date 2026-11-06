# Start-all: start AstrBot + NapCat.
# Run once. Idempotent via port-conflict checks.
# WebUI: http://127.0.0.1:6185 | NapCat WebUI: http://127.0.0.1:6099/webui
# OneBot reverse WS: ws://127.0.0.1:6199/ws (NapCat connects to AstrBot)
# Bot QQ number: 1178469499 (private chat to use extra_brain memory)
# IMPORTANT: console encoding must be UTF-8 so NapCat prints QR code as ASCII art directly.
#
# Launch mechanism note (updated 2026-11-06):
#   Uses Start-Process with visible console windows (needed for QR-code login and
#   live logs). Launched children are detached from the caller: on a normal
#   interactive terminal they keep running after this window closes.
#   WMI Win32_Process.Create was tried as a "detached" alternative and proved
#   unreliable on this machine (astrbot.exe exits immediately without the user's
#   environment; cmd.exe creation returns error 8), so it was removed.
#   After each launch the script waits for the service port and reports health,
#   so failures are visible instead of silent.

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'

# Force UTF-8 code page for the console so Chinese and QR code art render correctly.
chcp 65001 | Out-Null

$astrbotRoot   = 'D:\projects_software\astrbot-runtime'
$astrbotExe    = 'C:\Users\19758\miniconda3\envs\astrbot\Scripts\astrbot.exe'
$astrbotPort   = 6185
$shellRoot     = 'D:\projects_software\napcat\NapCat.52230.Shell'
$napcatVer     = '9.9.33-52230'
$napcatDir     = $shellRoot + '\versions\' + $napcatVer + '\resources\app\napcat'
$qqExe         = $shellRoot + '\QQ.exe'
$bootExe       = $shellRoot + '\NapCatWinBootMain.exe'
$hookDll       = $napcatDir + '\NapCatWinBootHook.dll'
$napcatPort    = 6099
$qqNumber      = ''

# API key: process env wins, else persisted User env (set once via install-autostart.ps1).
if (-not $env:DEEPSEEK_API_KEY) {
    $key = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
    if ($key) { $env:DEEPSEEK_API_KEY = $key }
}

# --- Port wait helper --------------------------------------------------------
function Wait-PortListening {
    param([int]$Port, [int]$TimeoutSeconds = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($c) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

# ============================== AstrBot ======================================
$listening = Get-NetTCPConnection -LocalPort $astrbotPort -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "[AstrBot] Already running (port $astrbotPort in use), skipping start." -ForegroundColor Yellow
} else {
    if (-not (Test-Path $astrbotExe)) {
        Write-Host "[AstrBot] ERROR: astrbot.exe not found: $astrbotExe" -ForegroundColor Red
    } else {
        if (-not $env:DEEPSEEK_API_KEY) {
            Write-Host '[AstrBot] Warning: DEEPSEEK_API_KEY is empty (falls back to config/user_config.json api.api_key).' -ForegroundColor Yellow
        }
        Write-Host "[AstrBot] Starting (workdir: $astrbotRoot)..." -ForegroundColor Cyan
        $p = Start-Process -FilePath $astrbotExe -ArgumentList 'run' `
             -WorkingDirectory $astrbotRoot -WindowStyle Normal -PassThru
        if (Wait-PortListening -Port $astrbotPort -TimeoutSeconds 90) {
            Write-Host "[AstrBot] Started (PID $($p.Id)), port $astrbotPort listening." -ForegroundColor Green
        } else {
            Write-Host "[AstrBot] WARNING: process launched (PID $($p.Id)) but port $astrbotPort not listening after 90s - check the AstrBot console window." -ForegroundColor Red
        }
    }
}
Write-Host "[AstrBot] WebUI: http://127.0.0.1:$astrbotPort   |   OneBot reverse WS: ws://127.0.0.1:6199/ws" -ForegroundColor Green

# ============================== NapCat =======================================
$listening = Get-NetTCPConnection -LocalPort $napcatPort -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "[NapCat] Already running (port $napcatPort in use), skipping start." -ForegroundColor Yellow
} else {
    if (-not (Test-Path $bootExe)) {
        Write-Host "[NapCat] ERROR: NapCatWinBootMain.exe not found: $bootExe" -ForegroundColor Red
    } else {
        if ($args.Count -gt 0) { $qqNumber = $args[0] }
        $env:NAPCAT_PATCH_PACKAGE = $napcatDir + '\qqnt.json'
        $env:NAPCAT_LOAD_PATH     = $napcatDir + '\loadNapCat.js'
        $env:NAPCAT_INJECT_PATH   = $hookDll
        $env:NAPCAT_MAIN_PATH     = $napcatDir + '\napcat.mjs'

        Write-Host "[NapCat] Starting (shell: $shellRoot)..." -ForegroundColor Cyan
        if ($qqNumber) {
            Write-Host "[NapCat] Quick login QQ number: $qqNumber" -ForegroundColor Cyan
        } else {
            Write-Host '[NapCat] Login QR code will print below as ASCII art in a UTF-8 console window - scan with mobile QQ.' -ForegroundColor Yellow
        }
        # Launch NapCat inside a cmd window with UTF-8 code page so the QR code ASCII art renders correctly.
        $napcatArgs = '/c chcp 65001 >nul && "' + $bootExe + '" "' + $qqExe + '" "' + $hookDll + '"'
        if ($qqNumber) { $napcatArgs += ' ' + $qqNumber }
        $p = Start-Process -FilePath 'cmd.exe' -ArgumentList $napcatArgs -WindowStyle Normal -PassThru
        if (Wait-PortListening -Port $napcatPort -TimeoutSeconds 60) {
            Write-Host "[NapCat] Started, port $napcatPort listening." -ForegroundColor Green
        } else {
            Write-Host "[NapCat] WARNING: launched but port $napcatPort not listening after 60s - QQ may need QR scan, see the NapCat console window." -ForegroundColor Red
        }
    }
}
Write-Host "[NapCat] WebUI: http://127.0.0.1:$napcatPort/webui   |   Reverse WS: ws://127.0.0.1:6199/ws (to AstrBot)" -ForegroundColor Green
Write-Host '[NapCat] Bot QQ number: 1178469499 (private chat to use extra_brain memory)' -ForegroundColor Green

# ============================== Summary ======================================
Write-Host '' -ForegroundColor Cyan
foreach ($p in @(6185, 6199, 6099)) {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($c) { Write-Host "  Port $p LISTENING (PID $($c[0].OwningProcess))" -ForegroundColor Green }
    else    { Write-Host "  Port $p not listening" -ForegroundColor Yellow }
}
Write-Host '' -ForegroundColor Cyan
Write-Host 'All started. To stop: .\scripts\stop-all.ps1' -ForegroundColor Cyan
Write-Host 'To check status: .\scripts\status-all.ps1' -ForegroundColor Cyan
