@echo off
setlocal

if not exist .venv\Scripts\activate.bat (
  echo ERROR: .venv not found. Run option 1 first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install sounddevice
python live_human_mic_asr_test.py --distance 2m --angle 0 --repeats 2 --record-seconds 5
pause
