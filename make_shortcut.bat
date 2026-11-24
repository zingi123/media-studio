@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (echo Run run.bat once first & pause & exit /b 1)
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\MediaStudio.lnk'); $s.TargetPath='%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments='main.py'; $s.WorkingDirectory='%~dp0'; $s.Save()"
echo Desktop shortcut "MediaStudio" created. Double-click it to open the app (no black window).
pause