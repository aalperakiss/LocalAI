@echo off
cd /d "%~dp0"
title OpenSourceAI MCP Mechanical - Pull model
set "MODEL=%~1"
if "%MODEL%"=="" set "MODEL=gpt-oss:20b"

echo Pulling %MODEL% into Ollama. Large models take a while to download.
echo.
ollama pull %MODEL%
if errorlevel 1 (
  echo.
  echo Pull failed. Is Ollama installed and running? Try: ollama list
  pause & exit /b 1
)
echo.
ollama list
echo.
echo Done. Pick %MODEL% from the Model list in the app sidebar.
pause
