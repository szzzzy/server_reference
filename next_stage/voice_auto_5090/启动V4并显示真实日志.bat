@echo off
call "%~dp0remove_autostart_silent.cmd"
timeout /t 2 /nobreak >nul
call "%~dp0start_v4_visible.cmd"
