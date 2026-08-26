@echo off
setlocal
set "PROJECT="
for /d %%D in ("%USERPROFILE%\Desktop\vr_test_5090_*") do if exist "%%~fD\.venv_vs\Scripts\python.exe" set "PROJECT=%%~fD"
if not defined PROJECT if exist "%~dp0..\..\.venv_vs\Scripts\python.exe" set "PROJECT=%~dp0..\.."
if not defined PROJECT (
  echo ERROR: Cannot find vr_test_5090 project with .venv_vs on Desktop.
  pause
  exit /b 1
)
if not exist "%PROJECT%\network\server\releases\manifest.json" (
  echo [first run] Generating test artifacts ^(firmware + audio + manifest^)...
  "%PROJECT%\.venv_vs\Scripts\python.exe" "%PROJECT%\network\server\make_test_artifacts.py"
)
rem --voice-mode real auto-switches to GPU env (.venv_5090_llm has torch/transformers)
set "PYEXE=%PROJECT%\.venv_vs\Scripts\python.exe"
echo %* | findstr /i "real" >nul && set "PYEXE=%PROJECT%\.venv_5090_llm\Scripts\python.exe"
echo Starting virtual server: MQTT broker + HTTPS file + WSS voice + MQTT control...
echo   python: %PYEXE%
echo   params: %*
echo Ctrl+C to stop. Type vcmd text + Enter to send (add --tty).
"%PYEXE%" "%PROJECT%\network\server\run_server.py" %*
pause
