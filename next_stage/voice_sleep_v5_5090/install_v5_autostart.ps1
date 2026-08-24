$ErrorActionPreference = 'Stop'

$voiceDir = $PSScriptRoot
$nextStage = Split-Path -Parent $voiceDir
$projectRoot = Split-Path -Parent $nextStage
$python = Join-Path $projectRoot '.venv_5090_llm\Scripts\python.exe'
$script = Join-Path $voiceDir 'voice_daemon.py'
$startup = [Environment]::GetFolderPath('Startup')
$v5Shortcut = Join-Path $startup 'VR Auto Guide V5.lnk'

if (-not (Test-Path -LiteralPath $python)) { throw "Python not found: $python" }
if (-not (Test-Path -LiteralPath $script)) { throw "V5 script not found: $script" }

# Stop current guide/TTS instances so no process keeps the board COM port.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like '*voice_auto_5090*voice_daemon.py*' -or
    $_.CommandLine -like '*voice_sleep_v5_5090*voice_daemon.py*' -or
    $_.CommandLine -like '*full_pipeline_auto_5090*tts_worker.py*'
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# Remove only old voice-guide startup shortcuts. The saved V4 files and ZIP
# remain untouched as a rollback baseline.
$shell = New-Object -ComObject WScript.Shell
Get-ChildItem -LiteralPath $startup -Filter '*.lnk' | ForEach-Object {
    $shortcut = $shell.CreateShortcut($_.FullName)
    if ($shortcut.Arguments -like '*voice_auto_5090*voice_daemon.py*' -or
        $shortcut.TargetPath -like '*voice_auto_5090*voice_daemon.py*' -or
        $_.Name -eq 'VR Auto Guide V4.lnk' -or $_.Name -eq 'VR Auto Guide V5.lnk') {
        Remove-Item -LiteralPath $_.FullName -Force
    }
}

$shortcut = $shell.CreateShortcut($v5Shortcut)
$shortcut.TargetPath = $python
$shortcut.Arguments = '-u "' + $script + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'VR Auto Guide V5 two-stage hardware wake'
$shortcut.Save()

Start-Process -FilePath $python -ArgumentList @('-u', $script) -WorkingDirectory $projectRoot -WindowStyle Hidden
Write-Host 'V5_AUTOSTART_INSTALLED=YES'
Write-Host "AUTOSTART=$v5Shortcut"
Write-Host 'V4 files were retained only as rollback backup.'
