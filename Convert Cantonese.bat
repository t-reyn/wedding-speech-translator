@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   Converting the Cantonese Whisper model for this PC.
echo   One-time, downloads ~1.6 GB. Do this on wifi, not at the venue.
echo ============================================================
echo.

rem Prefer the installer's virtualenv, fall back to a global Python 3.12, then PATH.
set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" convert_cantonese.py
echo.
echo Done. To use it: run "Start Captions" then pick the cantonese model,
echo or from a terminal:  python server.py --model cantonese
pause
