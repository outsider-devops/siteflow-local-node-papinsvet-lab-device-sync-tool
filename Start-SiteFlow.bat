@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 server.py --scan-on-start %*
) else (
  python server.py --scan-on-start %*
)
if errorlevel 1 (
  echo Install Python 3.10 or newer from python.org and enable Add Python to PATH.
  pause
)
