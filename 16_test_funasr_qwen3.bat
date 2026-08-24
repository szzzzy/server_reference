@echo off
setlocal
cd /d "%~dp0"
if not exist .venv_5090_llm\Scripts\python.exe (
  echo ERROR: Run menu option 1 first.
  pause
  exit /b 1
)
call .venv_5090_llm\Scripts\activate.bat
set "DEFAULT_WAV=recordings\board_20260819_221814\1m_0_s05_r1.wav"
set /p AUDIO=Input WAV path or press Enter for %DEFAULT_WAV%: 
if "%AUDIO%"=="" set "AUDIO=%DEFAULT_WAV%"
python test_funasr_qwen_pipeline.py --audio "%AUDIO%" --asr-model models\paraformer-zh-streaming --llm-model models\Qwen3-4B-Instruct-2507
pause
