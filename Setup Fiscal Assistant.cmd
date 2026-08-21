@echo off
title Setup RDC Fiscal Reference Assistant
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_fiscal_assistant.ps1"

