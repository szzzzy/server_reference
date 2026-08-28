@echo off
setlocal
cd /d "%~dp0"
echo ==========================================================
echo  Start VR voice server  -  REAL mode  -  WSS :9443
echo  Stop with Ctrl+C
echo ==========================================================
".venv_5090_llm\Scripts\python.exe" "virtual_server\run_server.py" --voice-mode real --tty %*
pause