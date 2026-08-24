@echo off
setlocal

echo [1/4] Looking for Python...
where python >nul 2>nul
if %errorlevel%==0 (
  set PYTHON_CMD=python
) else (
  where py >nul 2>nul
  if %errorlevel%==0 (
    set PYTHON_CMD=py -3
  ) else (
    echo ERROR: Python not found. Install Python 3.10 or use the 5090 server Python.
    pause
    exit /b 1
  )
)

echo [2/4] Creating local .venv...
%PYTHON_CMD% -m venv .venv
if errorlevel 1 (
  echo ERROR: failed to create .venv.
  pause
  exit /b 1
)

echo [3/4] Installing base packages...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements_base.txt
if errorlevel 1 (
  echo ERROR: base package install failed.
  pause
  exit /b 1
)

echo [4/4] Done.
echo Next: run option 2.
pause
