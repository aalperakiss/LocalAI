@echo off
cd /d "%~dp0"
set "PATH=%USERPROFILE%\anaconda3;%USERPROFILE%\anaconda3\Library\bin;%PATH%"
title OpenSourceAI MCP Mechanical - Setup

if not exist "config.json" (
  echo Creating config.json from config.example.json ...
  copy /y "config.example.json" "config.json" >nul
  echo.
  echo Open config.json and set mcp_servers.mechanical.command and args
  echo to your mechanical-mcp installation before starting the app.
  echo.
)

set "PY="
if exist "%USERPROFILE%\anaconda3\python.exe" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not defined PY for /f "delims=" %%p in ('where python 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY (
  echo Python not found. Install Python 3.10+ or Anaconda first.
  pause & exit /b 1
)
echo Python: %PY%

if not exist ".venv\Scripts\python.exe" (
  echo Creating .venv ...
  "%PY%" -m venv .venv || (echo Could not create .venv & pause & exit /b 1)
)
echo Installing libraries ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo pip install failed & pause & exit /b 1)

echo.
echo Running checks ...
echo.
".venv\Scripts\python.exe" check.py
echo.
echo Setup finished. Start the app with start.bat
pause
