@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0\..\.."

:menu
cls
echo ==========================================================
echo  RTX 5090 REALTIME VOICE PIPELINE
echo  BOARD - FUNASR - QWEN3 - COSYVOICE2 - SPEAKER
echo ==========================================================
echo.
echo  1. SELF CHECK
echo  2. START PIPELINE
echo  0. RETURN
echo.
set /p "choice=INPUT NUMBER: "
if "%choice%"=="1" goto self_check
if "%choice%"=="2" goto run
if "%choice%"=="0" exit /b 0
goto menu

:self_check
if not exist ".venv_5090_llm\Scripts\python.exe" goto no_llm
".venv_5090_llm\Scripts\python.exe" "next_stage\full_pipeline_5090\realtime_pipeline.py" --self-check
echo.
pause
goto menu

:run
if not exist ".venv_5090_llm\Scripts\python.exe" goto no_llm
".venv_5090_llm\Scripts\python.exe" "next_stage\full_pipeline_5090\realtime_pipeline.py"
echo.
pause
goto menu

:no_llm
echo MISSING .venv_5090_llm. RUN OPTION 1 IN THE MAIN MENU FIRST.
pause
goto menu
