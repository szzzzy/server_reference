@echo off
setlocal
cd /d "%~dp0"
set "LOG=%~dp0install_autostart.log"
echo [%date% %time%] Install started.>"%LOG%"
echo installing version 20260823-auto-reinsert-selfcheck-v4>"%~dp0voice_daemon.status.txt"
echo Installing voice daemon autostart...
call "%~dp0remove_autostart_silent.cmd"
timeout /t 2 /nobreak >nul
if not exist "%~dp0voice_daemon.vbs" goto missing_files
if not exist "%~dp0install_startup.ps1" goto missing_files
if not exist "%~dp0..\..\.venv_5090_llm\Scripts\python.exe" goto wrong_folder
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_startup.ps1" >>"%LOG%" 2>&1
if errorlevel 1 goto install_failed
echo Installation succeeded. Starting service now...
start "" "%WINDIR%\System32\wscript.exe" "%~dp0voice_daemon.vbs"
echo Old command-line pipeline instances have been stopped.
echo Keep the board connected and wait for model loading to finish.
echo Log: %LOG%
pause
exit /b 0
:wrong_folder
echo ERROR: Copy next_stage into the original 5090 project root first.
echo The project root must contain .venv_5090_llm.
echo ERROR: .venv_5090_llm not found.>>"%LOG%"
goto failed_pause
:missing_files
echo ERROR: Extract the ZIP first. Required files are missing.
echo ERROR: Required files missing.>>"%LOG%"
goto failed_pause
:install_failed
echo ERROR: Installation failed. Send me this log:
echo %LOG%
:failed_pause
pause
exit /b 1
