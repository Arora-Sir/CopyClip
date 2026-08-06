@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_copyclip.ps1" --log-level DEBUG
if errorlevel 1 (
    echo.
    echo CopyClip exited with an error. Press any key to close...
    pause >nul
)
endlocal
