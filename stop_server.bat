@echo off
rem Stop VR voice server (force). Normal stop = Ctrl+C in the server terminal.
rem Kills: run_server.py process (server) + tts_worker.py (CosyVoice subprocess if lingering).
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'run_server.py|tts_worker.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('killed PID ' + $_.ProcessId) }"
rem Show remaining listeners on server ports (should be empty)
netstat -ano | findstr ":9443" | findstr "LISTENING"
echo done - check above for leftovers (empty = stopped).