@echo off
setlocal
cd /d "%~dp0"
echo ==========================================================
echo  VR Voice Server - STUB mode, no models loaded
echo  WSS :9443/voice   HTTPS :8443   MQTT disabled
echo ==========================================================
set "PY=%VSF_STUB_PY%"
if defined PY goto :have
if exist "%~dp0.venv_vs\Scripts\python.exe" set "PY=%~dp0.venv_vs\Scripts\python.exe"
if not defined PY if exist "%~dp0..\.venv_vs\Scripts\python.exe" set "PY=%~dp0..\.venv_vs\Scripts\python.exe"
if not defined PY if exist "%~dp0.venv_5090_llm\Scripts\python.exe" set "PY=%~dp0.venv_5090_llm\Scripts\python.exe"
if not defined PY if exist "%~dp0..\.venv_5090_llm\Scripts\python.exe" set "PY=%~dp0..\.venv_5090_llm\Scripts\python.exe"
if defined PY goto :have
echo [ERROR] python env not found - need .venv_vs or .venv_5090_llm with websockets.
echo Fix 1. set env VSF_STUB_PY to python.exe
echo Fix 2. put .venv_vs or .venv_5090_llm in this package root or parent project root
pause
exit /b 1
:have
echo Using Python: %PY%
echo Starting server... Ctrl+C to stop.
"%PY%" "%~dp0server\run_server.py" --voice-mode stub %*
pause