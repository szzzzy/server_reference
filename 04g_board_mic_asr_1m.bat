@echo off
setlocal
cd /d C:\vr_test
if not exist .venv\Scripts\activate.bat (
  echo ERROR: .venv not found. Run option 1 first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install pyserial
python board_serial_asr_test.py --port COM7 --distance 1m --angle 0 --repeats 1 --record-seconds 6 --background-seconds 4 --endpoint-silence-ms 700 --output results/board_mic_asr_vad_results.csv
pause
