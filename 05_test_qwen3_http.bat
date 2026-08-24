@echo off
setlocal

if not exist .venv\Scripts\activate.bat (
  echo ERROR: .venv not found. Run option 1 first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
set /p URL=Input Qwen3 HTTP API URL, e.g. http://127.0.0.1:8000/chat:
python http_latency_test.py --url "%URL%" --text "Introduce the voice interaction workflow of a desktop AI robot in two sentences." --model-name "Qwen3_text_LLM" --output results\qwen3_http_latency.csv
pause
