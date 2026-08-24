@echo off
setlocal
set "PROJECT="
for /d %%D in ("%USERPROFILE%\Desktop\vr_test_5090_*") do if exist "%%~fD\.venv_5090_llm\Scripts\python.exe" set "PROJECT=%%~fD"
if not defined PROJECT set "PROJECT=%~dp0\..\.."
cd /d "%PROJECT%"
if not exist ".venv_5090_llm\Scripts\python.exe" exit /b 1
powershell.exe -NoProfile -Command "$p=Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" ^| Where-Object { $_.CommandLine -like '*next_stage*voice_auto_5090*voice_daemon.py*' }; if($p){exit 10}else{exit 0}"
if %ERRORLEVEL%==10 exit /b 0
".venv_5090_llm\Scripts\python.exe" -u "next_stage\voice_auto_5090\voice_daemon.py" >>"next_stage\voice_auto_5090\voice_daemon_current.log" 2>&1
exit /b %ERRORLEVEL%
