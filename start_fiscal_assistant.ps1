$ErrorActionPreference = "Stop"

$appDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $appDirectory

$port = if ($env:FISCAL_RAG_PORT) { $env:FISCAL_RAG_PORT } else { "8010" }
$pageUrl = "http://127.0.0.1:$port"

Write-Host "RDC Fiscal Reference Assistant" -ForegroundColor Green
Write-Host "Sources:  $(Resolve-Path ..\..)"
Write-Host "Open:     $pageUrl"
Write-Host "Press Ctrl+C to stop the server."

$pythonPath = if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
$serverProcess = Start-Process -FilePath $pythonPath -ArgumentList ".\server.py" -NoNewWindow -PassThru

try {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if ($serverProcess.HasExited) {
            throw "The fiscal assistant server stopped before it became ready."
        }
        try {
            Invoke-WebRequest -Uri "$pageUrl/api/status" -UseBasicParsing -TimeoutSec 1 | Out-Null
            break
        }
        catch {
            Start-Sleep -Milliseconds 200
        }
    }
    Start-Process $pageUrl
    Wait-Process -Id $serverProcess.Id
}
finally {
    if (-not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force
    }
}
