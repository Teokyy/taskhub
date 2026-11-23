@echo off
setlocal
cd /d "%~dp0"
title TaskHub Local

echo =====================================
echo TaskHub - local server
echo Folder: %CD%
echo =====================================

where py >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python launcher 'py' was not found.
  echo Python is installed, but the Windows launcher is unavailable.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -m venv .venv
  if errorlevel 1 (
    echo ERROR: Failed to create virtual environment.
    pause
    exit /b 1
  )
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
  echo ERROR: pip upgrade failed. Continuing to dependency installation...
)
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 (
  echo ERROR: Dependency installation failed.
  pause
  exit /b 1
)

echo.
echo Starting TaskHub at http://127.0.0.1:8000
echo Press CTRL+C to stop.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
