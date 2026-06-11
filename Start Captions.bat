@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "LOG=%~dp0captions_log.txt"
rem Avoid the multi-OpenMP-runtime access-violation crash (torch + ctranslate2 + numpy).
set "KMP_DUPLICATE_LIB_OK=TRUE"
set "OMP_NUM_THREADS=1"
set "MKL_NUM_THREADS=1"
set "FOR_DISABLE_CONSOLE_CTRL_HANDLER=1"
echo Starting Wedding Speech Translator (live mic)...
echo Models can take 10-30s to load on first run. Watch the browser tab.
echo All output is logged to: captions_log.txt
echo Keep this window open. Press Ctrl+C to stop for good.
echo.
start "" http://localhost:8765/
:loop
echo ---- server starting %date% %time% ---->> "%LOG%"
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -u server.py >> "%LOG%" 2>&1
echo ---- server exited %date% %time% ---->> "%LOG%"
echo.
echo Server stopped unexpectedly - restarting in 3s (close this window or Ctrl+C to quit)...
timeout /t 3 /nobreak >nul
goto loop
