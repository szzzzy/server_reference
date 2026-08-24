$ErrorActionPreference = 'Stop'

$voiceDir = $PSScriptRoot
$nextStage = Split-Path -Parent $voiceDir
$projectRoot = Split-Path -Parent $nextStage
$python = Join-Path $projectRoot '.venv_5090_llm\Scripts\python.exe'
$script = Join-Path $voiceDir 'voice_daemon.py'
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'VR Auto Guide V4.lnk'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment not found: $python"
}
if (-not (Test-Path -LiteralPath $script)) {
    throw "Voice program not found: $script"
}

# Stop only existing auto-guide instances before replacing the startup entry.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like '*voice_auto_5090*voice_daemon.py*'
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $python
$shortcut.Arguments = '-u "' + $script + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'VR Auto Guide V4'
$shortcut.Save()

Start-Process -FilePath $python -ArgumentList @('-u', $script) -WorkingDirectory $projectRoot -WindowStyle Hidden

Write-Host "PROJECT_ROOT=$projectRoot"
Write-Host "PYTHON=$python"
Write-Host "AUTOSTART=$shortcutPath"
Write-Host 'V4_STARTED=YES'
