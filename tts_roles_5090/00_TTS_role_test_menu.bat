@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PROJECT_ROOT=%~dp0.."
set "PYTHON_EXE=%PROJECT_ROOT%\.venv_5090_tts\Scripts\python.exe"

if not exist "%PYTHON_EXE%" goto no_python

:menu
cls
echo =====================================================
echo  CosyVoice2 Multi-role Test - RTX 5090
echo =====================================================
echo  1. Detect built-in speakers and official reference assets
echo  2. Download 3 fixed online role reference audio files
echo  3. Generate 9 role test WAV files
echo  4. Open NEW online references and result folders
echo  0. Exit
echo.
set /p choice=Input number: 

if "%choice%"=="1" "%PYTHON_EXE%" "%~dp0tts_role_test.py" probe
if "%choice%"=="2" goto online_references
if "%choice%"=="3" "%PYTHON_EXE%" "%~dp0tts_role_test.py" test
if "%choice%"=="4" goto open_folders
if "%choice%"=="0" exit /b 0
echo.
pause
goto menu

:online_references
"%PYTHON_EXE%" -c "import edge_tts" 2>nul
if errorlevel 1 "%PYTHON_EXE%" -m pip install edge-tts
if errorlevel 1 goto edge_tts_error
"%PYTHON_EXE%" -m pip install --upgrade edge-tts
if errorlevel 1 goto edge_tts_error
"%PYTHON_EXE%" "%~dp0generate_online_references.py"
echo.
pause
goto menu

:edge_tts_error
echo [ERROR] Could not install edge-tts. Check the network and retry option 2.
pause
goto menu

:open_folders
if not exist "%~dp0outputs\online_role_references" mkdir "%~dp0outputs\online_role_references"
if not exist "%~dp0results" mkdir "%~dp0results"
start "" "%~dp0outputs\online_role_references"
start "" "%~dp0results"
goto menu

:no_python
echo [ERROR] RTX 5090 TTS environment was not found:
echo %PYTHON_EXE%
echo Place the tts_roles_5090 folder inside the existing RTX 5090 project root.
pause
exit /b 1
