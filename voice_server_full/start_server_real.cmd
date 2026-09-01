@echo off
setlocal
cd /d "%~dp0"
echo ==========================================================
echo  VR Voice Server - REAL mode, ASR + Qwen3 + TTS
echo  WSS :9443/voice   HTTPS :8443   MQTT disabled
echo ==========================================================
set "PY=%VSF_REAL_PY%"
if defined PY goto :have
if exist "%~dp0.venv_5090_llm\Scripts\python.exe" set "PY=%~dp0.venv_5090_llm\Scripts\python.exe"
if not defined PY if exist "%~dp0..\.venv_5090_llm\Scripts\python.exe" set "PY=%~dp0..\.venv_5090_llm\Scripts\python.exe"
if defined PY goto :have
echo [ERROR] python env not found - need .venv_5090_llm with torch/transformers/funasr.
echo Fix 1. set env VSF_REAL_PY to python.exe
echo Fix 2. put .venv_5090_llm in this package root or parent project root
pause
exit /b 1
:have
echo Using Python: %PY%
echo Starting - model load 20-30 s, Ctrl+C to stop.
"%PY%" "%~dp0server\run_server.py" --voice-mode real --tty %*
pause