@echo off
REM Publish a code update.   release.bat 1.1.0 "what changed"
REM Publishes to the PUBLIC releases repo set below (your source repo can stay private).
REM Needs the GitHub CLI once:  winget install GitHub.cli   then   gh auth login
cd /d "%~dp0"
set RELEASES_REPO=zingi123/MediaStudio-releases
if "%~1"=="" (echo Usage: release.bat 1.1.0 "what changed" & exit /b 1)
if exist .venv311\Scripts\activate.bat call .venv311\Scripts\activate.bat
python tools\release.py %1 %2
if errorlevel 1 exit /b 1
gh release create v%1 --repo %RELEASES_REPO% release\update.zip release\update.zip.sha256 --title "MediaStudio %1" --notes-file release\NOTES.txt
if errorlevel 1 (echo Upload failed. Upload the 2 files in the release folder manually on GitHub, tag v%1 & pause & exit /b 1)
echo Published v%1 - every PC will get the update alert.
pause
