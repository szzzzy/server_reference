@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0\..\.."
if not exist ".venv_5090_llm\Scripts\python.exe" (
  echo 缺少原项目的 .venv_5090_llm，请把本目录放在原5090项目的 next_stage 下。
  pause
  exit /b 1
)
:restart
".venv_5090_llm\Scripts\python.exe" "next_stage\voice_daemon_5090\voice_daemon.py" >> "next_stage\voice_daemon_5090\voice_daemon.log" 2>&1
echo [%date% %time%] 服务退出，5秒后自动恢复。>> "next_stage\voice_daemon_5090\voice_daemon.log"
timeout /t 5 /nobreak >nul
goto restart
