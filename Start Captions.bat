@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "LOG=%~dp0captions_log.txt"
rem Avoid the multi-OpenMP-runtime access-violation crash (torch + ctranslate2 + numpy).
set "KMP_DUPLICATE_LIB_OK=TRUE"
set "OMP_NUM_THREADS=1"
set "MKL_NUM_THREADS=1"
set "FOR_DISABLE_CONSOLE_CTRL_HANDLER=1"

rem Prefer the installer's virtualenv, fall back to a global Python 3.12, then PATH.
set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting Wedding Speech Translator (live mic)...
echo.

rem Pick the Whisper model once, before the auto-restart loop (so a live event
rem never re-prompts). 10s with no key -> the accurate default (turbo).
echo Whisper model - smaller reacts faster but is less accurate:
echo   1) tiny   2) base   3) small   4) medium   5) turbo  (default, most accurate)
echo   6) cantonese  (turbo size - Cantonese-tuned; run "Convert Cantonese" once first)
choice /c 123456 /n /t 10 /d 5 /m "Choice [5], auto-continues in 10s: "
set "MODEL_ARG="
if errorlevel 6 ( set "MODEL_ARG=--model cantonese" & goto chosen )
if errorlevel 5 ( set "MODEL_ARG=" & goto chosen )
if errorlevel 4 ( set "MODEL_ARG=--model medium" & goto chosen )
if errorlevel 3 ( set "MODEL_ARG=--model small" & goto chosen )
if errorlevel 2 ( set "MODEL_ARG=--model base" & goto chosen )
if errorlevel 1 ( set "MODEL_ARG=--model tiny" & goto chosen )
:chosen
if defined MODEL_ARG ( echo Using %MODEL_ARG:--model =%. ) else ( echo Using default model ^(turbo^). )
echo.

echo Models can take 10-30s to load on first run. Watch the browser tab.
echo All output is logged to: captions_log.txt
echo Keep this window open. Press Ctrl+C to stop for good.
echo.
start "" http://localhost:8765/
:loop
echo ---- server starting %date% %time% ---->> "%LOG%"
"%PY%" -u server.py %MODEL_ARG% >> "%LOG%" 2>&1
echo ---- server exited %date% %time% ---->> "%LOG%"
echo.
echo Server stopped unexpectedly - restarting in 3s (close this window or Ctrl+C to quit)...
timeout /t 3 /nobreak >nul
goto loop
