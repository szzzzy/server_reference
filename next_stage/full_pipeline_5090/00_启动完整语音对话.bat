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
echo  3. TEST BOARD SPEAKER
echo  0. RETURN
echo.
set /p "choice=INPUT NUMBER: "
if "%choice%"=="1" goto self_check
if "%choice%"=="2" goto run
if "%choice%"=="3" goto speaker_test
if "%choice%"=="0" exit /b 0
goto menu

:self_check
if not exist ".venv_5090_llm\Scripts\python.exe" goto no_llm
call :ensure_serial
if errorlevel 1 goto serial_failed
".venv_5090_llm\Scripts\python.exe" "next_stage\full_pipeline_5090\realtime_pipeline.py" --self-check
echo.
pause
goto menu

:run
if not exist ".venv_5090_llm\Scripts\python.exe" goto no_llm
call :ensure_serial
if errorlevel 1 goto serial_failed
".venv_5090_llm\Scripts\python.exe" "next_stage\full_pipeline_5090\realtime_pipeline.py"
echo.
pause
goto menu

:no_llm
echo ERROR: THIS UPDATE PACKAGE IS NOT IN THE ORIGINAL 5090 PROJECT ROOT.
echo DO NOT REINSTALL. COPY THE PACKAGE CONTENTS INTO THE ORIGINAL PROJECT FOLDER.
echo THE SAME FOLDER MUST CONTAIN .venv_5090_llm AND 00_menu_5090.bat.
pause
goto menu

:speaker_test
if not exist ".venv_5090_llm\Scripts\python.exe" goto no_llm
call :ensure_serial
if errorlevel 1 goto serial_failed
".venv_5090_llm\Scripts\python.exe" "next_stage\full_pipeline_5090\speaker_test.py"
echo.
pause
goto menu

:ensure_serial
".venv_5090_llm\Scripts\python.exe" -c "import serial" >nul 2>nul
if not errorlevel 1 exit /b 0
echo INSTALLING PYSERIAL INTO THE EXISTING LLM ENVIRONMENT...
".venv_5090_llm\Scripts\python.exe" -m pip install pyserial
if errorlevel 1 exit /b 1
exit /b 0

:serial_failed
echo PYSERIAL INSTALL FAILED. CHECK NETWORK AND TRY AGAIN.
pause
goto menu
