@echo off
cd /d "%~dp0"
title MT5 Bot Dashboard (auto-restart)
:loop
python bot.py
echo.
echo [%date% %time%] bot berhenti (exit %errorlevel%), restart dalam 3 detik...
echo Catatan: jendela ini JANGAN ditutup selama bot dipakai.
timeout /t 3 /nobreak >nul
goto loop
