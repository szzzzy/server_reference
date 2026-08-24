$ErrorActionPreference = 'SilentlyContinue'

Write-Host 'Stopping the running V4 voice process to release the board COM port...'

Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like '*next_stage*voice_auto_5090*voice_daemon.py*' -or
    $_.CommandLine -like '*next_stage*voice_sleep_v5_5090*voice_daemon.py*' -or
    $_.CommandLine -like '*next_stage*full_pipeline_auto_5090*tts_worker.py*'
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
}

# The startup shortcut is deliberately retained. This only stops the current
# instance so V5 can be tested; V4 can run again after the next Windows login.
Start-Sleep -Milliseconds 800
Write-Host 'Current V4 process stopped. Its autostart shortcut was not deleted.'
