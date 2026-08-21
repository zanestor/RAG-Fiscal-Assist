$ErrorActionPreference = "Stop"

$appDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $appDirectory

Write-Host "Setting up RDC Fiscal Reference Assistant" -ForegroundColor Green

if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

if (-not (Test-Path -LiteralPath ".\.env")) {
    Copy-Item -LiteralPath ".\.env.example" -Destination ".\.env"
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Next: open .env, set OPENAI_API_KEY and/or OPENROUTER_API_KEY, then run 'Index Fiscal Documents.cmd'."
pause
