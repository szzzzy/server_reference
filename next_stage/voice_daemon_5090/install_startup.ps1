$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'VR Voice Guide.lnk'
$vbsPath = Join-Path $PSScriptRoot 'voice_daemon.vbs'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "$env:WINDIR\System32\wscript.exe"
$shortcut.Arguments = '"' + $vbsPath + '"'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Save()
Write-Host "Autostart shortcut created:" $shortcutPath
