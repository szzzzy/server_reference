@echo off
setlocal

if not exist .venv\Scripts\activate.bat (
  echo ERROR: .venv not found. Run option 1 first.
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat
echo.
echo Running realtime-like packet replay baseline...
echo Packet: 20ms, ASR window: 600ms, endpoint silence parameter record: 700ms
python realtime_paraformer_asr_eval.py --audio samples\standard_female_voice_16k_mono_16bit.wav --packet-ms 20 --asr-window-ms 600 --endpoint-silence-ms 700
pause
