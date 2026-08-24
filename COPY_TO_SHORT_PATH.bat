@echo off
setlocal

set TARGET=C:\vr_test

echo This will copy the test pack to:
echo %TARGET%
echo.
echo The short path avoids Windows path-length errors during PyTorch install.
echo.

if not exist "%TARGET%" mkdir "%TARGET%"

robocopy "%cd%" "%TARGET%" /E /XD ".venv" "results" "__pycache__" /XF "*.pyc"

echo.
echo Done.
echo Please open:
echo %TARGET%\00_menu.bat
echo.
echo In the new folder, run option 1, 2, 3 first if this is a new computer.
echo Today run option 4c, then 4d. Results are saved in %TARGET%\results.
pause
