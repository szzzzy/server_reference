@echo off
setlocal
cd /d "%~dp0"
if not exist .venv_5090_llm\Scripts\python.exe (
  echo ERROR: Run menu option 1 first.
  pause
  exit /b 1
)
call .venv_5090_llm\Scripts\activate.bat
python test_funasr_qwen_batch.py
pause
