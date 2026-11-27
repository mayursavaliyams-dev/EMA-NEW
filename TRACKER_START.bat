@echo off
rem ============================================================
rem  Stock Options + News tracker - ek click thi chalu karo.
rem  DISPLAY ONLY: koi order mukto nathi (paper mode).
rem  Data: fakt Dhan (5 connections).
rem ============================================================
setlocal
chcp 65001 >nul
title Stock Options Tracker (Dhan)
cd /d "%~dp0antigravity-py"

rem --- already running? just open the page ---
netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul
if not errorlevel 1 (
  echo Tracker pehla thi chalu chhe. Browser kholu chhu...
  start "" http://localhost:8000
  timeout /t 3 >nul
  exit /b 0
)

rem --- first time: create the Python box (.venv) and install ---
if exist ".venv\Scripts\python.exe" goto :have_venv
echo.
echo  Pehli var setup thay chhe. 2-5 minute lagse, rah juo...
echo.
set "PY="
py -3.12 -c "1" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.13 -c "1" >nul 2>&1 && set "PY=py -3.13"
if not defined PY py -3 -c "1" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "1" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo  [X] Aa computer par Python nathi. python.org thi Python 3.12 install karo,
  echo      install vakhte "Add python.exe to PATH" par tick karo.
  pause
  exit /b 1
)
%PY% -m venv .venv
if errorlevel 1 ( echo  [X] Setup nishfal. & pause & exit /b 1 )
:have_venv
if not exist ".venv\installed.ok" (
  ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
  if errorlevel 1 ( echo  [X] Packages install na thaya - internet check karo. & pause & exit /b 1 )
  echo ok> ".venv\installed.ok"
)

rem --- Dhan token: ask once if missing ---
if not exist "dhan.json" (
  ".venv\Scripts\python.exe" dhan_setup.py
  if errorlevel 1 ( pause & exit /b 1 )
)

rem --- safety settings: paper only, Dhan only, 5 connections ---
rem "TRACKER_START.bat now" = test right now even when the market is closed
set "ALWAYS=False"
if /i "%~1"=="now" set "ALWAYS=True"
set "DHAN_CLIENT_ID="
set "DHAN_ACCESS_TOKEN="
set "PYTHONIOENCODING=utf-8"
".venv\Scripts\python.exe" -c "import config; config.save_overrides({'TRADE_MODE': 'paper', 'trkProvider': 'dhan', 'trkDhanConnections': 5, 'trkAlwaysOn': %ALWAYS%})"

echo.
echo  ==========================================================
echo   Tracker chalu thay chhe...  Browser 15 second ma khulse.
echo   Tab: "Stock Options + News"
echo   Pehli var data download ma 2-3 minute lage ("building...").
echo.
echo   AA KALI WINDOW KHULI RAKHO. Bandh karso to tracker bandh.
echo  ==========================================================
echo.
start "" cmd /c "timeout /t 15 >nul & start http://localhost:8000"
".venv\Scripts\python.exe" main.py > tracker.log 2>&1
echo.
echo  Tracker bandh thayo. Problem hoy to tracker.log file mokalo.
pause
