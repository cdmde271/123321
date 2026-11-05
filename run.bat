@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"

rem --- locate a python interpreter, preferring the portable copy on the stick ---
set "PY="
if exist "%~dp0py\python.exe" set "PY=%~dp0py\python.exe"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [x] no python found on PATH and no portable copy in .\py\
  echo     drop python-3.12-embed-amd64.zip contents into .\py\ and retry
  pause
  exit /b 9009
)

if "%~1"=="" (
  set "MODE=probe"
) else (
  set "MODE=%~1"
)
shift

echo [i] interpreter: %PY%
echo [i] mode: %MODE%
%PY% "%~dp0strike.py" %MODE% %1 %2 %3 %4 %5 %6 %7 %8 %9
set "RC=%ERRORLEVEL%"
echo [i] exit=%RC%
pause
exit /b %RC%
