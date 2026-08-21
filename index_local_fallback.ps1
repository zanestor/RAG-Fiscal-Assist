$ErrorActionPreference = "Stop"

$appDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $appDirectory

$pythonPath = if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
    ".\.venv\Scripts\python.exe"
}
else {
    "python"
}

Write-Host "RAF Fiscal Assistant - Local fallback index" -ForegroundColor Green
Write-Host "Application: $appDirectory"
Write-Host "Generated data: $env:LOCALAPPDATA\RAF_Fiscal_Assistant"
Write-Host "Government & Public Affairs review-gated files remain excluded."
Write-Host ""

& $pythonPath -u ".\cli.py" index-local
if ($LASTEXITCODE -ne 0) {
    throw "Local indexing failed with exit code $LASTEXITCODE."
}

Write-Host ""
Write-Host "Local fallback index completed." -ForegroundColor Green

