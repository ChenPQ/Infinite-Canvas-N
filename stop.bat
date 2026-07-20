@echo off
echo Stopping Infinite-Canvas-N (port 3000)...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":3000" ^| findstr "LISTENING"') do (
    echo Killing PID %%a
    taskkill /F /PID %%a 2>nul
)
echo Done.
timeout /t 3 /nobreak >nul
