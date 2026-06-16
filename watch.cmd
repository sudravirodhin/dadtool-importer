@echo off
REM Start the auto-import watcher. Drop audio files into audio\pending\ and they
REM get transcoded + analyzed + written automatically. Ctrl+C to stop.
REM Double-click this file, or run "watch" from a CMD prompt in this folder.
cd /d "%~dp0"
echo Dead as Disco - song importer watcher
echo Drop audio files into:  %~dp0audio\pending
echo Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -m dadtool.cli watch %*
pause
