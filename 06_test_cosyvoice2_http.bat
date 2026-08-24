@echo off
setlocal

if not exist .venv\Scripts\activate.bat (
  echo ERROR: .venv not found. Run option 1 first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
set /p URL=Input CosyVoice2 HTTP API URL, e.g. http://127.0.0.1:9880/tts:
python http_latency_test.py --url "%URL%" --text "Hello, this is the desktop AI robot voice interaction test." --model-name "CosyVoice2_TTS" --output results\cosyvoice2_http_latency.csv
pause
