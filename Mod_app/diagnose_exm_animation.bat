@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 diagnose_exm_animation.py
) else (
  python diagnose_exm_animation.py
)
echo.
echo Check the animation_diagnostics folder beside this batch file.
pause
