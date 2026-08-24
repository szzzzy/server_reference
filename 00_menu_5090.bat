@echo off
setlocal
cd /d "%~dp0"

:menu
cls
echo ==========================================================
echo  RTX 5090 Voice Pipeline Test Menu
echo  FunASR - Qwen3/Qwen2.5 - CosyVoice2
echo ==========================================================
echo.
echo  1. Create LLM/FunASR environment
echo  2. Download Qwen3-4B-Instruct-2507
echo  3. Download Qwen2.5-1.5B-Instruct
echo  4. Download Paraformer-online for FunASR
echo  5. Test Qwen3 text model
echo  6. Test Qwen2.5 text model
echo  7. Test FunASR to Qwen3 pipeline with a WAV file
echo  8. Create CosyVoice2 environment and install source
echo  9. Download CosyVoice2-0.5B
echo 10. Test CosyVoice2 TTS
echo 11. Build result summary
echo 12. Open result and audio folders
echo 13. Create small transfer ZIP (exclude envs and models)
echo 14. Run realtime board ASR - Qwen3 - CosyVoice2 pipeline
echo  0. Exit
echo.
set /p choice=Input number: 

if "%choice%"=="1" call 10_create_llm_env.bat
if "%choice%"=="2" call 11_download_qwen3.bat
if "%choice%"=="3" call 12_download_qwen25.bat
if "%choice%"=="4" call 13_download_paraformer.bat
if "%choice%"=="5" call 14_test_qwen3.bat
if "%choice%"=="6" call 15_test_qwen25.bat
if "%choice%"=="7" call 16_test_funasr_qwen3.bat
if "%choice%"=="8" call 20_create_cosyvoice2_env.bat
if "%choice%"=="9" call 21_download_cosyvoice2.bat
if "%choice%"=="10" call 22_test_cosyvoice2.bat
if "%choice%"=="11" call 30_build_5090_summary.bat
if "%choice%"=="12" (
  if not exist results_5090 mkdir results_5090
  if not exist outputs_5090 mkdir outputs_5090
  start "" "%cd%\results_5090"
  start "" "%cd%\outputs_5090"
)
if "%choice%"=="13" call 40_make_5090_transfer_zip.bat
if "%choice%"=="14" goto realtime_pipeline
if "%choice%"=="0" exit /b 0
goto menu

:realtime_pipeline
if not exist "%~dp0next_stage\full_pipeline_5090\00_start_full_pipeline.bat" goto missing_realtime_pipeline
call "%~dp0next_stage\full_pipeline_5090\00_start_full_pipeline.bat"
goto menu

:missing_realtime_pipeline
echo.
echo ERROR: REALTIME PIPELINE FILE IS MISSING:
echo %~dp0next_stage\full_pipeline_5090\00_start_full_pipeline.bat
echo.
echo EXTRACT THE FIX ZIP INTO THIS PROJECT ROOT AND REPLACE SAME-NAME FILES.
pause
goto menu
