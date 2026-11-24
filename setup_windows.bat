@echo off
setlocal enabledelayedexpansion
REM MediaStudio setup for Windows. It REUSES an existing virtual environment (.venv311 / .venv / the active one),
REM installs only what is missing, never upgrades or removes working packages, and never changes PyTorch by itself.
cd /d "%~dp0"
set "PY="
if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" set "PY=%VIRTUAL_ENV%\Scripts\python.exe"
if not defined PY if exist ".venv311\Scripts\python.exe" set "PY=.venv311\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  where python >nul 2>nul || (echo Python not found. Install Python 3.11 from python.org and tick "Add to PATH". & pause & exit /b 1)
  echo No virtual environment found - creating .venv311 ...
  python -m venv .venv311 || (echo Could not create the virtual environment. & pause & exit /b 1)
  set "PY=.venv311\Scripts\python.exe"
)
echo Using: %PY%
"%PY%" --version
echo.
echo === 1/4 Environment backup (pip freeze) ===
if not exist "%USERPROFILE%\MediaStudio\env_backup" mkdir "%USERPROFILE%\MediaStudio\env_backup"
"%PY%" -m pip list --format=freeze > "%USERPROFILE%\MediaStudio\env_backup\freeze_before_setup.txt" 2>nul
echo Saved to %USERPROFILE%\MediaStudio\env_backup\freeze_before_setup.txt
echo.
echo === 2/4 Core packages (only the missing ones are installed) ===
"%PY%" -m pip install -r requirements.txt || (echo Core install failed. & pause & exit /b 1)
echo.
echo === 3/4 Optional AI packages ===
echo Already-installed packages are NOT reinstalled or upgraded. PyTorch is never touched here.
set /p AI="Install the missing optional AI packages from requirements-ai.txt? [y/N] "
if /i "!AI!"=="y" "%PY%" -m pip install -r requirements-ai.txt
set /p VOICE="Install voice-type / clone extras (requirements-voice.txt)? [y/N] "
if /i "!VOICE!"=="y" "%PY%" -m pip install -r requirements-voice.txt
echo.
echo === 4/4 Diagnosis (nothing is changed) ===
"%PY%" verify_env.py --report "%USERPROFILE%\MediaStudio\environment_report.txt"
echo.
echo If PyTorch / TorchAudio / Pyannote show a COMPATIBILITY problem run:   %PY% verify_env.py --plan
echo (it explains the cause and proposes a fix; applying it asks for your confirmation and saves a backup first)
echo Start the app with run.bat
pause
