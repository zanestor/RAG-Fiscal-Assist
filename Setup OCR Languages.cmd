@echo off
title RAF Fiscal Assistant - Setup OCR Languages
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_ocr_languages.ps1"
echo.
pause
