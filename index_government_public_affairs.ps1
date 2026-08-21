$ErrorActionPreference = "Stop"

$appDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $appDirectory

$pythonPath = if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
    ".\.venv\Scripts\python.exe"
}
else {
    "python"
}

Write-Host "RAF Fiscal Assistant - Government & Public Affairs" -ForegroundColor Green
Write-Host "This source may contain personal or operational records." -ForegroundColor Yellow
Write-Host "It will be added to the local fallback index with the explicit review override."
Write-Host "Do not run another indexing command at the same time."
Write-Host ""

& $pythonPath -u ".\cli.py" index-local --source government_public_affairs --include-review-required
if ($LASTEXITCODE -ne 0) {
    throw "Government & Public Affairs indexing failed with exit code $LASTEXITCODE."
}

Write-Host ""
Write-Host "Government & Public Affairs indexing completed." -ForegroundColor Green

