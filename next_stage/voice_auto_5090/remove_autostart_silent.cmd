@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0remove_autostart.ps1" >nul 2>nul
