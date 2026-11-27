@echo off
rem ============================================================
rem  Ek var chalavo: tracker roj Somvar-Shukravar savare 7:30 vagye
rem  potani rite chalu thase (Windows Task Scheduler).
rem  - PC 7:30 e bandh hoy to, chalu kari login karo etle tarat chalu thase.
rem  - 3 divas pachhi bandh nahi thay (time limit nathi).
rem  Bandh karva: AUTO_START_REMOVE.bat
rem ============================================================
setlocal
chcp 65001 >nul
title Auto start 7:30
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0antigravity-py\auto_start.ps1" -Bat "%~dp0TRACKER_START.bat"
if errorlevel 1 (
  echo.
  echo  [X] Schedule set na thayu. Aa file par Right-click karo ane "Run as administrator" pasand karo.
  pause
  exit /b 1
)
echo.
echo  [OK] Have tracker roj Somvar-Shukravar savare 7:30 vagye potani rite chalu thase.
pause
