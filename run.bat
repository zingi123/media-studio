@echo off
cd /d "%~dp0"
REM Uses the project's existing virtual environment: .venv311 first (your setup), then .venv, then the active one.
if exist ".venv311\Scripts\python.exe" (set "PY=.venv311\Scripts\python.exe") else if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
"%PY%" main.py
if errorlevel 1 pause
