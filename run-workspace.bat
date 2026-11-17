@echo off
rem Power BI team workspace on this computer only.
rem   run-workspace.bat add-folder "C:\Users\me\Contoso\BI - Reports"
rem   run-workspace.bat scan
rem   run-workspace.bat                 (no arguments: opens http://127.0.0.1:8766)
rem The database is pbi-workspace.db next to this file unless DAXLIN_WORKSPACE_DB says otherwise.
setlocal
cd /d "%~dp0"
if not defined DAXLIN_WORKSPACE_DB set "DAXLIN_WORKSPACE_DB=%~dp0pbi-workspace.db"
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.9 or later is needed for the workspace: https://www.python.org/downloads/
  pause
  exit /b 1
)
if "%~1"=="" (
  %PY% -m dax_lineage workspace serve
) else (
  %PY% -m dax_lineage workspace %*
)
if errorlevel 1 pause
