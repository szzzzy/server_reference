@echo off
setlocal
cd /d "%~dp0"
if not exist .venv_5090_tts\Scripts\python.exe (
  where py >nul 2>nul
  if errorlevel 1 (
    echo ERROR: Python launcher not found. Install 64-bit Python 3.10.
    pause
    exit /b 1
  )
  py -3.10 -m venv .venv_5090_tts
)
if not exist .venv_5090_tts\Scripts\python.exe (
  echo ERROR: CosyVoice2 requires Python 3.10 for this test pack.
  pause
  exit /b 1
)
call .venv_5090_tts\Scripts\activate.bat
python -m pip install --upgrade pip setuptools wheel
python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
python -m pip install modelscope huggingface_hub soundfile numpy pandas nvidia-ml-py
if not exist third_party\CosyVoice\.git (
  if not exist third_party mkdir third_party
  git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git third_party\CosyVoice
)
if not exist third_party\CosyVoice\.git (
  echo ERROR: CosyVoice source download failed. Check Git and network.
  pause
  exit /b 1
)
git -C third_party\CosyVoice submodule update --init --recursive
python -m pip install -r third_party\CosyVoice\requirements.txt
pause
