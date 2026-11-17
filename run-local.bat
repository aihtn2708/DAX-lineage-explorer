@echo off
rem DAX lineage explorer on this computer only: double-click this file.
rem Opens http://127.0.0.1:8765 in your browser. Close this window to stop it.
rem Needs Python 3.9 or later; without it, the offline page opens instead.
setlocal
cd /d "%~dp0"
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=python"
if not defined PY goto offline
%PY% -m dax_lineage serve %*
if errorlevel 1 pause
goto :eof

:offline
echo Python 3.9 or later was not found, so the offline page opens instead.
echo It works the same and needs no Python: docs\dax-lineage-explorer.html
start "" "%~dp0docs\dax-lineage-explorer.html"
timeout /t 5 >nul
