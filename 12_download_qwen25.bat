@echo off
setlocal
cd /d "%~dp0"
if not exist .venv_5090_llm\Scripts\python.exe (
  echo ERROR: Run menu option 1 first.
  pause
  exit /b 1
)
call .venv_5090_llm\Scripts\activate.bat
python download_5090_model.py --model-id Qwen/Qwen2.5-1.5B-Instruct --local-dir models\Qwen2.5-1.5B-Instruct
pause
