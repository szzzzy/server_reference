@echo off
setlocal

if not exist .venv\Scripts\activate.bat (
  echo ERROR: .venv not found. Run option 1 first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python test_paraformer_streaming_asr.py --audio samples\standard_female_voice_16k_mono_16bit.wav
pause
