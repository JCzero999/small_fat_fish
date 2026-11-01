# Start-silent: unattended launch of AstrBot + NapCat (no console windows).
# Designed to be run by Windows Scheduled Task (logon + periodic watchdog).
# Idempotent: each service is skipped when its port is already listening.
# Visible/interactive variant: use start-all.ps1 (QR code login needs a console).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\start-silent.ps1
#   DEEPSEEK_API_KEY comes from the User environment (set once) or process env.
#
# Bot QQ: 1178469499 quick-login (requires an existing saved QQ login session).

$ErrorActionPreference = 'Stop'

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
$qqNumber      = '1178469499'
$logDir        = Join-Path $astrbotRoot 'data\logs\autostart'

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# API key: process env wins, else persisted User env (set via install-autostart.ps1).
$key = $env:DEEPSEEK_API_KEY
if (-not $key) { $key = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User') }
if ($key) { $env:DEEPSEEK_API_KEY = $key }

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

# ---- AstrBot ----
$astrbotListening = Get-NetTCPConnection -LocalPort $astrbotPort -State Listen -ErrorAction SilentlyContinue
if ($astrbotListening) {
    Write-Host "[$stamp] [AstrBot] already running, skip."
} else {
    $stdout = Join-Path $logDir ('astrbot-' + (Get-Date -Format 'yyyyMMdd') + '.out.log')
    $stderr = Join-Path $logDir ('astrbot-' + (Get-Date -Format 'yyyyMMdd') + '.err.log')
    $psi = Start-Process -FilePath $astrbotExe -ArgumentList 'run' `
        -WorkingDirectory $astrbotRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    Write-Host "[$stamp] [AstrBot] started pid=$($psi.Pid) (log: $stdout)"
}

# ---- NapCat (quick login; requires saved QQ session) ----
$napcatListening = Get-NetTCPConnection -LocalPort $napcatPort -State Listen -ErrorAction SilentlyContinue
if ($napcatListening) {
    Write-Host "[$stamp] [NapCat] already running, skip."
} else {
    $env:NAPCAT_PATCH_PACKAGE = $napcatDir + '\qqnt.json'
    $env:NAPCAT_LOAD_PATH     = $napcatDir + '\loadNapCat.js'
    $env:NAPCAT_INJECT_PATH   = $hookDll
    $env:NAPCAT_MAIN_PATH     = $napcatDir + '\napcat.mjs'

    $stdout = Join-Path $logDir ('napcat-' + (Get-Date -Format 'yyyyMMdd') + '.out.log')
    $stderr = Join-Path $logDir ('napcat-' + (Get-Date -Format 'yyyyMMdd') + '.err.log')
    $cmdLine = '"' + $bootExe + '" "' + $qqExe + '" "' + $hookDll + '" ' + $qqNumber
    $psi = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c ' + $cmdLine `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    Write-Host "[$stamp] [NapCat] started pid=$($psi.Pid) quick-login $qqNumber (log: $stdout)"
}
