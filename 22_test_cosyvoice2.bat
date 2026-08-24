@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv_5090_tts\Scripts\python.exe" goto no_python
if not exist "%~dp0models\CosyVoice2-0.5B" goto no_model
set "PYTHONPATH=%~dp0third_party\CosyVoice;%~dp0third_party\CosyVoice\third_party\Matcha-TTS"
"%~dp0.venv_5090_tts\Scripts\python.exe" "%~dp0test_cosyvoice2_local.py" --model-dir "%~dp0models\CosyVoice2-0.5B" --prompt-wav "%~dp0recordings\board_20260819_221814\1m_0_s05_r1.wav"
pause
exit /b 0
:no_python
echo ERROR: TTS Python environment not found.
pause
exit /b 1
:no_model
echo ERROR: CosyVoice2 model not found. Run menu option 9 first.
pause
exit /b 1
