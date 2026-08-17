@echo off
cd /d "%~dp0.."

echo Stamping a pre-sleep baseline copy...
python -c "import winclip; winclip.write_text('pre-sleep-baseline')"

echo.
echo Starting the clipboard listener probe.
echo Leave this window open, then let the laptop sleep as normal.
echo When you log back in, copy some text, then let Claude know.
echo.

python spike\win_clipboard_listener_probe.py

echo.
echo Probe stopped (exit code %ERRORLEVEL%). Press any key to close this window.
pause >nul
