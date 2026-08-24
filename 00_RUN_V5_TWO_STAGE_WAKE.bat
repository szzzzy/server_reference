@echo off
setlocal
set "PROJECT="
for /d %%D in ("%USERPROFILE%\Desktop\vr_test_5090_*") do if exist "%%~fD\.venv_5090_llm\Scripts\python.exe" set "PROJECT=%%~fD"
if not defined PROJECT if exist "%~dp0.venv_5090_llm\Scripts\python.exe" set "PROJECT=%~dp0"
if not defined PROJECT (
  echo ERROR: Cannot find vr_test_5090 project on Desktop.
  pause
  exit /b 1
)
call "%PROJECT%\next_stage\voice_sleep_v5_5090\start_v5_visible.cmd"
