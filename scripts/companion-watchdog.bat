@echo off
rem extra_brain companion bot keep-alive (launched from user Startup folder)
rem Idempotent silent start + 5-minute watchdog. See watchdog.ps1.
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0watchdog.ps1"
