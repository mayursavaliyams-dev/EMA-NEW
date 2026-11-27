@echo off
rem  MOCK TEST: nakli market par aakhu tracker chalave (Dhan/broker ne adto pan nathi).
rem  Dashboard: http://localhost:8001   (real tracker 8000 par chale toy vandho nathi)
setlocal
chcp 65001 >nul
title MOCK TEST (nakli bhav)
cd /d "%~dp0antigravity-py"
if not exist ".venv\Scripts\python.exe" (
  echo  Pehla TRACKER_START.bat ek var chalavo.
  pause
  exit /b 1
)
set "PYTHONIOENCODING=utf-8"
echo.
echo  MOCK TEST chalu thay chhe - NAKLI BHAV, koi order nahi.
echo  Browser 10 second ma khulse. Bandh karva aa window bandh karo.
echo.
start "" cmd /c "timeout /t 10 >nul & start http://localhost:8001"
".venv\Scripts\python.exe" mock_market.py > mock.log 2>&1
pause
