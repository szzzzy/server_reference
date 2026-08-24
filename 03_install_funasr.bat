@echo off
setlocal

if not exist .venv\Scripts\activate.bat (
  echo ERROR: .venv not found. Run option 1 first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
echo Installing PyTorch CUDA build...
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126
if errorlevel 1 (
  echo ERROR: PyTorch install failed.
  echo If this is the 5090 server and cu126 fails, install the newest PyTorch CUDA build from pytorch.org, then run this option again.
  pause
  exit /b 1
)

echo Installing FunASR and ModelScope packages...
pip install -U funasr modelscope
if errorlevel 1 (
  echo ERROR: FunASR install failed.
  pause
  exit /b 1
)
echo Done. Next: run option 4.
pause
