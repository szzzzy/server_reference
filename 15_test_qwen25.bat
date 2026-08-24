@echo off
setlocal
cd /d "%~dp0"
call .venv_5090_llm\Scripts\activate.bat
python test_local_qwen.py --model-dir models\Qwen2.5-1.5B-Instruct --model-name Qwen2.5-1.5B-Instruct
pause
