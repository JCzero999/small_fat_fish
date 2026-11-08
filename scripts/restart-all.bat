@echo off
chcp 65001 >nul
rem Launcher for restart-all.ps1 - double-click friendly (no ExecutionPolicy prompt).
rem Argument 1 (optional): QQ number for NapCat quick login.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart-all.ps1" %1
echo.
pause
