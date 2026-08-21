@echo off
title RAF Fiscal Assistant - Local Fallback Index
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0index_local_fallback.ps1"
echo.
pause
