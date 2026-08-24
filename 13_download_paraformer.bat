@echo off
setlocal
cd /d "%~dp0"
if not exist .venv_5090_llm\Scripts\python.exe (
  echo ERROR: Run menu option 1 first.
  pause
  exit /b 1
)
call .venv_5090_llm\Scripts\activate.bat
python download_5090_model.py --model-id iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online --hf-model-id funasr/paraformer-zh-streaming --local-dir models\paraformer-zh-streaming
pause
