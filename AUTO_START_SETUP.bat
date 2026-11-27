@echo off
rem ============================================================
rem  Ek var chalavo: tracker roj Somvar-Shukravar savare 7:30 vagye
rem  potani rite chalu thase (Windows Task Scheduler).
rem  Computer chalu ane login hovu joie. Bandh karva: AUTO_START_REMOVE.bat
rem ============================================================
setlocal
chcp 65001 >nul
title Auto start 7:30
schtasks /create /tn "Stock Options Tracker 0730" /tr "\"%~dp0TRACKER_START.bat\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 07:30 /f
if errorlevel 1 (
  echo.
  echo  [X] Schedule set na thayu. Aa file par Right-click karo ane "Run as administrator" pasand karo.
  pause
  exit /b 1
)
echo.
echo  [OK] Have tracker roj Somvar-Shukravar savare 7:30 vagye potani rite chalu thase.
echo       Computer 7:30 pehla chalu ane login karelu rakhjo.
pause
