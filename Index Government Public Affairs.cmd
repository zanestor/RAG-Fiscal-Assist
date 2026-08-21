@echo off
title RAF Fiscal Assistant - Government and Public Affairs
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0index_government_public_affairs.ps1"
echo.
pause
