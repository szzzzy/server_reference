@echo off
setlocal
cd /d "%~dp0"
call .venv_5090_llm\Scripts\activate.bat
python test_local_qwen.py --model-dir models\Qwen3-4B-Instruct-2507 --model-name Qwen3-4B-Instruct-2507
pause
