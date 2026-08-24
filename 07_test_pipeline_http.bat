@echo off
setlocal

if not exist .venv\Scripts\activate.bat (
  echo ERROR: .venv not found. Run option 1 first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
echo This script tests your full HTTP pipeline: audio - ASR - Qwen3 - CosyVoice2.
set /p URL=Input full pipeline HTTP API URL, e.g. http://127.0.0.1:9000/pipeline:
python http_latency_test.py --url "%URL%" --audio samples\standard_female_voice_16k_mono_16bit.wav --audio-seconds 60 --text "Recognize the audio and reply briefly." --model-name "FunASR_Qwen3_CosyVoice2_pipeline" --output results\pipeline_http_latency.csv
pause
