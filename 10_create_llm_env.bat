@echo off
setlocal
cd /d "%~dp0"
if exist .venv_5090_llm\Scripts\python.exe goto install

where py >nul 2>nul
if not errorlevel 1 (
  py -3.11 -m venv .venv_5090_llm 2>nul
  if not exist .venv_5090_llm\Scripts\python.exe py -3.10 -m venv .venv_5090_llm
) else (
  python -m venv .venv_5090_llm
)
if not exist .venv_5090_llm\Scripts\python.exe (
  echo ERROR: Python 3.10 or 3.11 is required.
  pause
  exit /b 1
)

:install
call .venv_5090_llm\Scripts\activate.bat
python -m pip install --upgrade pip setuptools wheel
python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r requirements_5090_llm.txt
python check_5090_env.py
pause
