@echo off
cd /d "%~dp0"
set "PATH=%USERPROFILE%\anaconda3;%USERPROFILE%\anaconda3\Library\bin;%PATH%"
".venv\Scripts\python.exe" check.py
pause
