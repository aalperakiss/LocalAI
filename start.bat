@echo off
cd /d "%~dp0"
set "PATH=%USERPROFILE%\anaconda3;%USERPROFILE%\anaconda3\Library\bin;%PATH%"
title Alper - OpenSource AI Model
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause & exit /b 1
)

tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if errorlevel 1 (
  if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
    echo Starting Ollama ...
    start "" /min "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
    timeout /t 4 /nobreak >nul
  ) else (
    echo Ollama is not running. Start it from the Start menu.
  )
)

echo Open your model in Mechanical and start its gRPC server, then enter the port
echo it returns in the sidebar and press Connect.
echo The browser opens at http://127.0.0.1:8100 . Close this window to stop the app.
".venv\Scripts\python.exe" app.py
pause
