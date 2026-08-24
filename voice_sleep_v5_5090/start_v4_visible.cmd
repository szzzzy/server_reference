@echo off
setlocal
set "PROJECT="
for /d %%D in ("%USERPROFILE%\Desktop\vr_test_5090_*") do if exist "%%~fD\.venv_5090_llm\Scripts\python.exe" set "PROJECT=%%~fD"
if not defined PROJECT set "PROJECT=%~dp0\..\.."
cd /d "%PROJECT%"
title VR AUTO GUIDE V4 - DO NOT CLOSE
echo ======================================================
echo VR AUTO GUIDE VERSION: 20260823-auto-reinsert-selfcheck-v4
echo PROJECT ROOT: %CD%
echo ======================================================
if not exist ".venv_5090_llm\Scripts\python.exe" (
  echo ERROR: .venv_5090_llm was not found.
  pause
  exit /b 1
)
echo Starting V4 directly. Model loading output follows...
".venv_5090_llm\Scripts\python.exe" -u "next_stage\voice_auto_5090\voice_daemon.py"
echo.
echo V4 STOPPED. EXIT CODE: %ERRORLEVEL%
pause
