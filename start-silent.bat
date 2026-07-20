@echo off
cd /d "%~dp0"
start /min "" python\pythonw.exe main.py
echo Infinite-Canvas-N is starting...
echo Visit: http://127.0.0.1:3000/
timeout /t 3 /nobreak >nul
