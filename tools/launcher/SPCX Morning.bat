@echo off
setlocal enabledelayedexpansion
title SPCX Morning
cd /d "%~dp0"

rem Everything runs from this folder. The scripts import one another, so
rem they have to sit together -- see the README beside this file.

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python was not found on the PATH.
    echo   Install it from python.org and tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo.
    echo   No .env file in this folder. Without it nothing can reach
    echo   Alpaca or your phone. It needs:
    echo.
    echo     ALPACA_API_KEY=...
    echo     ALPACA_SECRET_KEY=...
    echo     PUSHOVER_APP_TOKEN=...
    echo     PUSHOVER_USER_KEY=...
    echo.
    pause
    exit /b 1
)

:menu
cls
echo.
echo   ================================================
echo     SPCX MORNING
echo   ================================================
echo.
echo     1   Start the morning      candles from 08:55, MACD from 09:40
echo     2   Candles only           08:55 to 10:00
echo     3   MACD alerts only       logs from 09:30, alerts from 09:40
echo.
echo     A   SPCX alarms            day and night, up and down, TO YOUR PHONE
echo.
echo     4   Test my phone          one quiet, one alarm
echo     5   Replay a past day      full tape, costs nothing
echo.
echo     6   Today's signals        what has fired so far
echo     7   End of day             fill in what price did next
echo     8   PDF report             one session as charts, opens when done
echo.
echo     0   Quit
echo.
set "choice="
set /p "choice=  Choose: "

if "%choice%"=="1" goto both
if "%choice%"=="2" goto candles
if "%choice%"=="3" goto alerts
if /i "%choice%"=="A" goto alarms
if "%choice%"=="4" goto testpush
if "%choice%"=="5" goto replay
if "%choice%"=="6" goto recent
if "%choice%"=="7" goto endofday
if "%choice%"=="8" goto report
if "%choice%"=="0" exit /b 0
goto menu

:both
echo.
echo   Opening two windows. Close either to stop that half.
start "SPCX candles" cmd /k python open_candles.py
timeout /t 2 /nobreak >nul
start "SPCX MACD" cmd /k python spcx_alert.py --watch
echo   Both started. This menu stays open.
echo.
pause
goto menu

:candles
start "SPCX candles" cmd /k python open_candles.py
goto menu

:alerts
start "SPCX MACD" cmd /k python spcx_alert.py --watch
goto menu

:alarms
rem The one option that SENDS. Alarms only (no quiet updates), up and
rem down alike; the first ring of a new move repeats on the phone until
rem it is acknowledged. Started late, it sends nothing from before it began.
rem --overnight: 20:00-03:50, Sunday to Thursday nights, from IB Gateway.
rem Keep IB Gateway logged in; if it is not, the phone is told.
start "SPCX alarms" cmd /k python open_candles.py --from 04:00 --until 20:00 --rings-only --persist 30 --overnight
goto menu

:testpush
cls
echo.
python spcx_alert.py --test-push
echo.
pause
goto menu

:replay
cls
echo.
echo   Which day? Use the form 2026-09-18. A weekday the market was open.
echo.
set "day="
set /p "day=  Date: "
if "%day%"=="" goto menu
echo.
python open_candles.py --replay %day% --dry-run
echo.
pause
goto menu

:report
cls
echo.
echo   Which day? Use the form 2026-09-18, or press Enter for the last one.
echo.
set "rday="
set /p "rday=  Date: "
echo.
if "%rday%"=="" (python daily_report.py) else (python daily_report.py --date %rday%)
echo.
pause
goto menu

:recent
cls
echo.
python spcx_alert.py --recent 30
echo.
pause
goto menu

:endofday
cls
echo.
echo   Filling in what price did after each signal...
python spcx_alert.py --backfill
echo.
python spcx_alert.py --recent 30
echo.
pause
goto menu
