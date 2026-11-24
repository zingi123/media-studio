@echo off
REM ONE command: publish update to users + save code to git.
REM   publish                      -> next version automatically (1.0.0 -> 1.0.1), notes "update"
REM   publish 1.5.0                -> chosen version
REM   publish 1.5.0 "what changed" -> chosen version + notes
setlocal
cd /d "%~dp0"
set RELEASES_REPO=zingi123/media-studio-releases
set "VER=%~1"
set "MSG=%~2"
if "%VER%"=="" set "VER=auto"
if "%MSG%"=="" set "MSG=update"
if exist .venv311\Scripts\activate.bat call .venv311\Scripts\activate.bat

echo === 1/3 Build update.zip ===
python tools\release.py "%VER%" "%MSG%"
if errorlevel 1 (echo FAILED: build update.zip & pause & exit /b 1)
set /p NEWVER=<release\VERSION.txt

echo === 2/3 Upload Release v%NEWVER% to %RELEASES_REPO% ===
gh release create v%NEWVER% --repo %RELEASES_REPO% release\update.zip release\update.zip.sha256 --title "MediaStudio %NEWVER%" --notes-file release\NOTES.txt
if errorlevel 1 (echo FAILED: upload release. Run "gh auth login" first. & pause & exit /b 1)

echo === 3/3 Save code to git ===
git add .
git commit -m "v%NEWVER% %MSG%"
git push

echo.
echo DONE: v%NEWVER% published. Users will get the update alert.
pause
