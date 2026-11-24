@echo off
REM Build MediaStudio.exe (run ONCE, or when you add a new library). Normal code changes do NOT need this: use release.bat.
cd /d "%~dp0"
if exist .venv311\Scripts\activate.bat call .venv311\Scripts\activate.bat
python -m pip install pyinstaller
python tools\build_exe.py
if errorlevel 1 (echo BUILD FAILED & pause & exit /b 1)
echo.
echo Next: open installer.iss with Inno Setup and press Compile -> installer\MediaStudio_Setup.exe
pause
