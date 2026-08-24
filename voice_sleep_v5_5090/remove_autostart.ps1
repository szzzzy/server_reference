$ErrorActionPreference = 'SilentlyContinue'

$startup = [Environment]::GetFolderPath('Startup')
$englishLinks = @(
    (Join-Path $startup 'VR Voice Guide.lnk'),
    (Join-Path $startup 'VR Voice Auto Guide.lnk')
)
foreach ($englishLink in $englishLinks) {
    if (Test-Path -LiteralPath $englishLink) {
        Remove-Item -LiteralPath $englishLink -Force
    }
}

# Remove any older shortcut created by this project without placing its
# Chinese name in a CMD file, where legacy code pages can corrupt parsing.
Get-ChildItem -LiteralPath $startup -Filter '*.lnk' | ForEach-Object {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($_.FullName)
    if ($shortcut.Arguments -like '*voice_daemon*' -or
        $shortcut.TargetPath -like '*voice_daemon*') {
        Remove-Item -LiteralPath $_.FullName -Force
    }
}

$pidFile = Join-Path $PSScriptRoot 'voice_daemon.pid'
if (Test-Path -LiteralPath $pidFile) {
    $daemonPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    Stop-Process -Id $daemonPid -Force
    Remove-Item -LiteralPath $pidFile -Force
}

# Stop the hidden CMD watchdog as well; otherwise it restarts Python five
# seconds later and can keep the serial port occupied after an upgrade.
Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object {
    $_.CommandLine -like '*start_voice_daemon.cmd*' -or
    $_.CommandLine -like '*启动语音常驻版.bat*' -or
    $_.CommandLine -like '*full_pipeline_5090*' -or
    $_.CommandLine -like '*00_menu_5090*'
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
}

Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like '*voice_daemon.py*' -or
    $_.CommandLine -like '*full_pipeline_5090*realtime_pipeline.py*'
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
}

Write-Host 'Autostart removed. The old voice daemon has been stopped.'
