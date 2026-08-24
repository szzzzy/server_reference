@echo off
setlocal

:menu
cls
echo ===============================================
echo  Voice Robot Pipeline Test Pack
echo  Main: Paraformer-online - Qwen3 - CosyVoice2
echo  Env: local .venv, no conda required
echo ===============================================
echo.
echo  1. Create .venv and install base packages
echo  2. Check Python / GPU environment
echo  3. Install PyTorch + FunASR
echo  4. Test Paraformer-online ASR
echo  4p. Prepare short ASR samples for real CER
echo  4a. Batch ASR accuracy + command hit-rate
echo  4b. ASR stability repeat test
echo  4c. Realtime-like packet replay baseline
echo  4d. Realtime parameter sweep
echo  4e. Live human microphone ASR test 1m
echo  4f. Live human microphone ASR test 2m
echo  4g. Board microphone serial PCM to ASR test 1m
echo  4h. Board microphone serial PCM to ASR test 2m
echo  4s. Test SenseVoiceSmall ASR contrast
echo  5. Test Qwen3 HTTP API
echo  6. Test CosyVoice2 HTTP API
echo  7. Test full pipeline HTTP API
echo  8. Monitor GPU memory for 60 seconds
echo  9. Open results folder
echo  0. Exit
echo.
set /p choice=Input number:

if "%choice%"=="1" call 01_create_venv.bat
if "%choice%"=="2" call 02_check_env.bat
if "%choice%"=="3" call 03_install_funasr.bat
if "%choice%"=="4" call 04_test_paraformer_asr.bat
if /I "%choice%"=="4p" call 04_prepare_short_asr_samples.bat
if /I "%choice%"=="4a" call 04a_batch_asr_accuracy.bat
if /I "%choice%"=="4b" call 04b_stability_asr_test.bat
if /I "%choice%"=="4c" call 04c_realtime_stream_baseline.bat
if /I "%choice%"=="4d" call 04d_realtime_param_sweep.bat
if /I "%choice%"=="4e" call 04e_live_human_mic_1m.bat
if /I "%choice%"=="4f" call 04f_live_human_mic_2m.bat
if /I "%choice%"=="4g" call 04g_board_mic_asr_1m.bat
if /I "%choice%"=="4h" call 04h_board_mic_asr_2m.bat
if /I "%choice%"=="4s" call 04s_test_sensevoice_asr.bat
if "%choice%"=="5" call 05_test_qwen3_http.bat
if "%choice%"=="6" call 06_test_cosyvoice2_http.bat
if "%choice%"=="7" call 07_test_pipeline_http.bat
if "%choice%"=="8" call 08_monitor_gpu_60s.bat
if "%choice%"=="9" start "" "%cd%\results"
if "%choice%"=="0" exit /b 0

goto menu
