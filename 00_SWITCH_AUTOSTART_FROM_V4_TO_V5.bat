@echo off
setlocal
set "PROJECT="
for /d %%D in ("%USERPROFILE%\Desktop\vr_test_5090_*") do if exist "%%~fD\.venv_5090_llm\Scripts\python.exe" set "PROJECT=%%~fD"
if not defined PROJECT if exist "%~dp0.venv_5090_llm\Scripts\python.exe" set "PROJECT=%~dp0"
if not defined PROJECT (
  echo ERROR: Cannot find the 5090 project.
  pause
  exit /b 1
)
cd /d "%PROJECT%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "next_stage\voice_sleep_v5_5090\install_v5_autostart.ps1"
echo.
echo V5 is now the only voice-guide autostart version.
echo V4 remains only in the saved rollback ZIP.
pause
