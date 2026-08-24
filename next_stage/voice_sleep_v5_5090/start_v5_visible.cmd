@echo off
setlocal
set "PROJECT="
for /d %%D in ("%USERPROFILE%\Desktop\vr_test_5090_*") do if exist "%%~fD\.venv_5090_llm\Scripts\python.exe" set "PROJECT=%%~fD"
if not defined PROJECT if exist "%~dp0\..\..\.venv_5090_llm\Scripts\python.exe" set "PROJECT=%~dp0\..\.."
if not defined PROJECT (
  echo ERROR: Cannot find the 5090 project containing .venv_5090_llm.
  echo Expected: %USERPROFILE%\Desktop\vr_test_5090_*\
  pause
  exit /b 1
)
cd /d "%PROJECT%"
title VR AUTO GUIDE V5 TWO-STAGE WAKE - DO NOT CLOSE
echo ======================================================
echo VR AUTO GUIDE VERSION: 20260823-two-stage-hardware-wake-v5
echo PROJECT ROOT: %CD%
echo ======================================================
if not exist ".venv_5090_llm\Scripts\python.exe" (
  echo ERROR: .venv_5090_llm was not found.
  pause
  exit /b 1
)
echo Starting visible V5. This does not replace the working V4 autostart.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "next_stage\voice_sleep_v5_5090\stop_v4_for_v5_test.ps1"
".venv_5090_llm\Scripts\python.exe" -u "next_stage\voice_sleep_v5_5090\voice_daemon.py"
echo.
echo V5 STOPPED. EXIT CODE: %ERRORLEVEL%
pause
