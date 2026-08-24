@echo off
setlocal
cd /d "%~dp0"
if exist .venv_5090_llm\Scripts\python.exe (
  call .venv_5090_llm\Scripts\activate.bat
  python summarize_5090_results.py
) else (
  python summarize_5090_results.py
)
pause
