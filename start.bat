@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WeCom-Claude Bot
echo Starting WeCom to Claude Code bridge...
echo Close this window to stop the service.
echo.
python bot.py 2>> "%~dp0bot.err.log"
echo.
echo Service exited. Press any key to close.
pause >nul
