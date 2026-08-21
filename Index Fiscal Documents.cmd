@echo off
title Index RDC Fiscal Documents
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" cli.py index
) else (
    python cli.py index
)

echo.
echo Indexing finished or stopped. Review the summary above.
pause

