# Start-all: start AstrBot + NapCat.
# Run once. Idempotent via port-conflict checks.
# WebUI: http://127.0.0.1:6185 | NapCat WebUI: http://127.0.0.1:6099/webui
# OneBot reverse WS: ws://127.0.0.1:6199/ws (NapCat connects to AstrBot)
# Bot QQ number: 1178469499 (private chat to use extra_brain memory)
# IMPORTANT: console encoding must be UTF-8 so NapCat prints QR code as ASCII art directly.

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
$deepseekApiKey = $env:DEEPSEEK_API_KEY
$qqNumber      = ''

# AstrBot
$listening = Get-NetTCPConnection -LocalPort $astrbotPort -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "[AstrBot] Already running (port $astrbotPort in use), skipping start." -ForegroundColor Yellow
} else {
    if (-not $deepseekApiKey) {
        Write-Host '[AstrBot] Warning: DEEPSEEK_API_KEY is empty, memory consolidation unavailable (read-only mode).' -ForegroundColor Yellow
        Write-Host '[AstrBot] Suggestion: $env:DEEPSEEK_API_KEY="<key>"; .\scripts\start-all.ps1' -ForegroundColor Cyan
    } else {
        $env:DEEPSEEK_API_KEY = $deepseekApiKey
    }
    Write-Host "[AstrBot] Starting (workdir: $astrbotRoot)..." -ForegroundColor Cyan
    Start-Process -FilePath $astrbotExe -ArgumentList 'run' -WorkingDirectory $astrbotRoot -WindowStyle Normal
    Write-Host '[AstrBot] Launch started, see the console window for output.' -ForegroundColor Green
}
Write-Host "[AstrBot] WebUI: http://127.0.0.1:$astrbotPort   |   OneBot reverse WS: ws://127.0.0.1:6199/ws" -ForegroundColor Green

# NapCat
$listening = Get-NetTCPConnection -LocalPort $napcatPort -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "[NapCat] Already running (port $napcatPort in use), skipping start." -ForegroundColor Yellow
} else {
    $env:NAPCAT_PATCH_PACKAGE = $napcatDir + '\qqnt.json'
    $env:NAPCAT_LOAD_PATH     = $napcatDir + '\loadNapCat.js'
    $env:NAPCAT_INJECT_PATH   = $napcatDir + '\NapCatWinBootHook.dll'
    $env:NAPCAT_MAIN_PATH     = $napcatDir + '\napcat.mjs'

    if ($args.Count -gt 0) { $qqNumber = $args[0] }
    $argList = @($qqExe, $hookDll)
    if ($qqNumber) { $argList += $qqNumber }

    Write-Host "[NapCat] Starting (shell: $shellRoot)..." -ForegroundColor Cyan
    if ($qqNumber) {
        Write-Host "[NapCat] Quick login QQ number: $qqNumber" -ForegroundColor Cyan
    } else {
        Write-Host '[NapCat] Login QR code will print below as ASCII art in a UTF-8 console window - scan with mobile QQ.' -ForegroundColor Yellow
    }
    # Launch NapCat inside a cmd window with UTF-8 code page so the QR code ASCII art renders correctly.
    $napcatArgs = '/c chcp 65001 >nul && "' + $bootExe + '" "' + $qqExe + '" "' + $hookDll + '"'
    if ($qqNumber) { $napcatArgs += ' ' + $qqNumber }
    Start-Process -FilePath 'cmd.exe' -ArgumentList $napcatArgs -WindowStyle Normal
    Write-Host '[NapCat] Launch started, see the console window for QR code and output.' -ForegroundColor Green
}
Write-Host "[NapCat] WebUI: http://127.0.0.1:$napcatPort/webui   |   Reverse WS: ws://127.0.0.1:6199/ws (to AstrBot)" -ForegroundColor Green
Write-Host '[NapCat] Bot QQ number: 1178469499 (private chat to use extra_brain memory)' -ForegroundColor Green

Write-Host '' -ForegroundColor Cyan
Write-Host 'All started. To stop: .\scripts\stop-all.ps1' -ForegroundColor Cyan
Write-Host 'To check status: .\scripts\status-all.ps1' -ForegroundColor Cyan
