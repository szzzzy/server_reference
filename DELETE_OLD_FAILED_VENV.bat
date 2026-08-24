@echo off
setlocal

set OLD=%~dp0.venv

echo This will delete the old failed virtual environment:
echo "%OLD%"
echo.
echo It will NOT delete C:\vr_test and will NOT delete samples/results/scripts.
echo.
set /p OK=Type YES to delete:

if /I not "%OK%"=="YES" (
  echo Cancelled.
  pause
  exit /b 0
)

if exist "%OLD%" (
  rmdir /S /Q "%OLD%"
  echo Deleted old .venv.
) else (
  echo Old .venv not found. Nothing to delete.
)

pause
