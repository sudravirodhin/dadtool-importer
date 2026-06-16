@echo off
REM Run any dadtool command from this directory:  dad <command> [args]
REM e.g.  dad batch   |   dad rename   |   dad preview "Song Name"
cd /d "%~dp0"
".venv\Scripts\python.exe" -m dadtool.cli %*
