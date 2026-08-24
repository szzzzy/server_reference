@echo off
setlocal

if not exist .venv\Scripts\activate.bat (
  echo ERROR: .venv not found. Run option 1 first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python monitor_nvidia_smi.py --duration 60 --interval 1 --output results\gpu_memory_60s.csv
pause
