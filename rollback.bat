@echo off
cd /d "%~dp0"
call .venv\Scripts\activate
python -m app.shared.app_updater rollback
pause