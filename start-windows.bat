@echo off
REM RealHands - start the local bridge. Double-click this file.
REM First run sets up a small Python environment (one minute). After that it's instant.
REM Requires Python 3.10+ (https://www.python.org/downloads/ - check "Add to PATH").

cd /d "%~dp0bridge" || goto :err

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required. Install it from https://www.python.org/downloads/
  echo During install, check "Add Python to PATH". Then try again.
  pause
  exit /b 1
)

if not exist .venv (
  echo First run - setting things up ^(a minute or two^)...
  python -m venv .venv
  REM bridge core, then the chat deps (litellm) so the side-panel chat works out of the box.
  .venv\Scripts\pip install -q -r requirements.txt
  if errorlevel 1 (
    echo Setup failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
  .venv\Scripts\pip install -q -r ..\vision\requirements.txt
  if errorlevel 1 (
    echo Setup failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
)

echo.
echo   RealHands is running.
echo   Bridge:  http://localhost:7878
echo   Leave this window open while you use it. Close it to stop.
echo.
.venv\Scripts\uvicorn bridge:app --host 127.0.0.1 --port 7878
goto :eof

:err
echo Could not find the bridge folder.
pause
exit /b 1
