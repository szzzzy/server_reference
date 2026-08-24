@echo off
setlocal
cd /d "%~dp0\..\.."
set "STATUS=next_stage\voice_auto_5090\voice_daemon.status.txt"
set "LOG=next_stage\voice_auto_5090\voice_daemon_current.log"
if not exist ".venv_5090_llm\Scripts\python.exe" (
  echo launcher_error - .venv_5090_llm was not found in %CD%>"%STATUS%"
  exit /b 1
)
echo starting - version 20260823-auto-reinsert-selfcheck-v4>"%STATUS%"
echo [%date% %time%] Starting voice daemon.>"%LOG%"
".venv_5090_llm\Scripts\python.exe" -u "next_stage\voice_auto_5090\voice_daemon.py" >>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo stopped - exit_code=%RC% - version 20260823-auto-reinsert-selfcheck-v4>"%STATUS%"
echo [%date% %time%] Service stopped with exit code %RC%.>>"%LOG%"
