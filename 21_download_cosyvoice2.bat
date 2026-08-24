@echo off
setlocal
cd /d "%~dp0"
if not exist .venv_5090_tts\Scripts\python.exe (
  echo ERROR: Run menu option 8 first.
  pause
  exit /b 1
)
call .venv_5090_tts\Scripts\activate.bat
python download_5090_model.py --model-id iic/CosyVoice2-0.5B --hf-model-id FunAudioLLM/CosyVoice2-0.5B --local-dir models\CosyVoice2-0.5B
pause
