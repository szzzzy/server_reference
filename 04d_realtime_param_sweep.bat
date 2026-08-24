@echo off
setlocal

if not exist .venv\Scripts\activate.bat (
  echo ERROR: .venv not found. Run option 1 first.
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat
set "AUDIO=samples\standard_female_voice_16k_mono_16bit.wav"
set "REFERENCE=samples\standard_female_voice_16k_mono_16bit.txt"
if not exist "%REFERENCE%" (
  echo ERROR: Missing complete reference transcript: %REFERENCE%
  echo Put the official verbatim transcript there before testing.
  pause
  exit /b 1
)
echo.
echo Running realtime parameter sweep...
python realtime_paraformer_asr_eval.py --audio "%AUDIO%" --expected-file "%REFERENCE%" --packet-ms 20 --asr-window-ms 300 --endpoint-silence-ms 600
python realtime_paraformer_asr_eval.py --audio "%AUDIO%" --expected-file "%REFERENCE%" --packet-ms 20 --asr-window-ms 600 --endpoint-silence-ms 700
python realtime_paraformer_asr_eval.py --audio "%AUDIO%" --expected-file "%REFERENCE%" --packet-ms 40 --asr-window-ms 600 --endpoint-silence-ms 700
python realtime_paraformer_asr_eval.py --audio "%AUDIO%" --expected-file "%REFERENCE%" --packet-ms 40 --asr-window-ms 900 --endpoint-silence-ms 800
python summarize_realtime_results.py
pause
