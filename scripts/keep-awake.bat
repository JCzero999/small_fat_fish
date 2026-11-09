@echo off
rem keep-awake.bat — 双击启动「系统常驻」守护（后台隐藏窗口）。
rem 已经在跑就不重复启动。
setlocal
set PS1=%~dp0keep-awake.ps1

powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*keep-awake.ps1*' -and $_.Name -eq 'powershell.exe' }) { Write-Output 'keep-awake already running'; exit 0 } else { exit 1 }"
if %errorlevel%==0 (
    echo keep-awake is already running.
    goto :eof
)

powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%PS1%"
echo keep-awake launched (hidden window).
