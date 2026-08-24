@echo off
setlocal
cd /d "%~dp0"
echo ================= VOICE SERVICE STATUS =================
if exist "voice_daemon.status.txt" (
  type "voice_daemon.status.txt"
) else (
  echo status file not found - service has not started
)
echo.
echo ================= LAST LOG LINES =======================
if exist "voice_daemon_current.log" (
  powershell.exe -NoProfile -Command "Get-Content -LiteralPath '%~dp0voice_daemon_current.log' -Tail 40"
) else (
  echo voice_daemon_current.log not found
)
echo.
pause
