@echo off
rem Runs the trading bot server and restarts it if it stops (for example when IB Gateway
rem isn't logged in yet). Output goes to logs\bot.log. Trading itself stays STOPPED until
rem you press Start on the dashboard or in Telegram.
cd /d "%~dp0.."
if not exist logs mkdir logs
:loop
echo [%date% %time%] starting server >> logs\bot.log
python run.py >> logs\bot.log 2>&1
echo [%date% %time%] server exited (code %errorlevel%), restarting in 60s >> logs\bot.log
timeout /t 60 /nobreak >nul
goto loop
