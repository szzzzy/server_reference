@echo off
setlocal

if not exist .venv\Scripts\activate.bat (
  echo ERROR: .venv not found. Run option 1 first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python batch_paraformer_asr_eval.py --cases asr_cases.csv --commands command_cases.csv
pause
