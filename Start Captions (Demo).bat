@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Wedding Speech Translator demo (scripted captions, no mic/models needed)...
echo.
start "" http://localhost:8765/
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -u server.py --demo
echo.
echo Server stopped.
pause
