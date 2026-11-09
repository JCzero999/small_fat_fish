@echo off
chcp 65001 >nul
rem Restart web-side only: Chat Shell (8756) + AstrBot (6185/6199). QQ/NapCat stay up
rem and reconnect automatically (ws reconnectInterval=5000). Use this when you changed
rem src/extra_brain, the AstrBot plugin, or cmd_config.json - avoid re-logging QQ.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart-all.ps1" -SkipQQ
echo.
pause
