@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 run.py gui
) else (
  python run.py gui
)
if errorlevel 1 pause

