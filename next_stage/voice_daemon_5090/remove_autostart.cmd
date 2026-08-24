@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0remove_autostart.ps1"
if errorlevel 1 (
  echo ERROR: Removal failed.
  pause
  exit /b 1
)
pause
