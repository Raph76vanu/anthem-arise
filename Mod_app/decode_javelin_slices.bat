@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag your Anthem installation folder onto this file, or pass it as the first argument.
  echo Put ranger.caspart, storm.caspart, colossus.caspart, and interceptor.caspart beside this file.
  pause
  exit /b 2
)
for %%J in (ranger storm colossus interceptor) do (
  if exist "%%J.caspart" (
    py -3 run.py decode-frostbite-slice "%%J.caspart" "%%J.meshset" --game-root "%~1"
    if errorlevel 1 exit /b 1
  )
)
echo Decoding complete. The JSON above lists each Javelin's real geometry chunk IDs.
pause
