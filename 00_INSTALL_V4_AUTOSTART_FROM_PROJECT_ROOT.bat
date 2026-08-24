@echo off
setlocal
title VR AUTO GUIDE V4 FINAL AUTOSTART
cd /d "%~dp0"
echo PROJECT ROOT: %CD%
if not exist ".venv_5090_llm\Scripts\python.exe" (
  echo ERROR: Extract this ZIP directly into the original 5090 project folder.
  echo The current folder does not contain .venv_5090_llm.
  pause
  exit /b 1
)
if not exist "next_stage\voice_auto_5090\install_absolute_startup.ps1" (
  echo ERROR: V4 files are incomplete.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\next_stage\voice_auto_5090\install_absolute_startup.ps1"
if errorlevel 1 (
  echo INSTALL FAILED.
  pause
  exit /b 1
)
echo.
echo INSTALL SUCCEEDED. V4 IS RUNNING HIDDEN.
echo Keep the board connected and wait for model loading.
pause
