@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Prefer the installer's virtualenv, fall back to a global Python 3.12, then PATH.
set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting Wedding Speech Translator demo (scripted captions, no mic/models needed)...
echo.
start "" http://localhost:8765/
"%PY%" -u server.py --demo
echo.
echo Server stopped.
pause
