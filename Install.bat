@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Wedding Speech Translator - Installer
echo ============================================================
echo   Wedding Speech Translator  -  one-click installer (Windows)
echo ============================================================
echo This sets up Python packages and downloads the models (~4 GB).
echo It only needs to be done once. Leave it running.
echo.

rem ---- 1. find or install Python ----
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY ( py -3 --version >nul 2>&1 && set "PY=py -3" )
if not defined PY ( python --version >nul 2>&1 && set "PY=python" )

if not defined PY (
  echo Python was not found. Installing Python 3.12 ...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo  ^>^>  Python is now installed, but this window can't see it yet.
  echo  ^>^>  Please CLOSE this window and double-click Install.bat again.
  echo.
  pause
  exit /b 0
)
echo Using Python: %PY%
echo.

rem ---- 2. create a clean virtual environment ----
if exist venv (
  echo Removing previous environment...
  rmdir /s /q venv
)
echo Creating virtual environment...
%PY% -m venv venv
if errorlevel 1 ( echo. & echo Could not create the environment. & pause & exit /b 1 )
call venv\Scripts\activate.bat

rem ---- 3. install Python packages ----
echo.
echo Installing packages (a few minutes, lots of output is normal)...
python -m pip install --upgrade pip
python -m pip install -r requirements-windows.txt
if errorlevel 1 ( echo. & echo Package install FAILED. Check your internet and re-run Install.bat. & pause & exit /b 1 )

rem ---- 4. optional NVIDIA GPU acceleration ----
where nvidia-smi >nul 2>&1
if %errorlevel%==0 (
  echo.
  echo NVIDIA GPU detected - installing GPU acceleration ^(3-4x faster^)...
  python -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
)

rem ---- 5. download + build the models ----
echo.
echo Downloading models ^(~4 GB, one-time^). This can take a while.
echo If your connection drops it resumes automatically - just wait.
python setup_models.py
if errorlevel 1 ( echo. & echo Model download did not finish - re-run Install.bat to resume. & pause & exit /b 1 )

rem ---- 6. Desktop shortcut (best-effort; don't fail the install if it errors) ----
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $l=$ws.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Wedding Captions.lnk'); $l.TargetPath='%~dp0Start Captions.bat'; $l.WorkingDirectory='%~dp0'; $l.Save()" 2>nul

echo.
echo ============================================================
echo   All done!  Double-click the "Wedding Captions" shortcut on
echo   your Desktop ^(or "Start Captions.bat" in this folder^) - it
echo   opens a control page in your browser where you pick your
echo   model + microphone and click Start.
echo   ^(or "Start Captions (Demo).bat" to preview the display^)
echo ============================================================
pause
