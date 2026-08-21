@echo off
title RDC Fiscal Reference Assistant
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python is required to start the fiscal assistant.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_fiscal_assistant.ps1"

