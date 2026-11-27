@echo off
rem  Dhan token badlo (roj savare, ke "token expired" aave tyare).
setlocal
chcp 65001 >nul
title Dhan token
cd /d "%~dp0antigravity-py"
if not exist ".venv\Scripts\python.exe" (
  echo  Pehla TRACKER_START.bat ek var chalavo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" dhan_setup.py
echo.
echo  Tracker chalu hoy to e 1 minute ma potani rite navo token vapri fari jodashe.
pause
