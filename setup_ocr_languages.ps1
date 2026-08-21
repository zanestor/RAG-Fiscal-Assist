param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$targetDirectory = Join-Path $env:LOCALAPPDATA "RAF_Fiscal_Assistant\tessdata"
$installedDirectory = "C:\Program Files\Tesseract-OCR\tessdata"
$frenchUrl = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/fra.traineddata"

New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null

foreach ($language in @("eng", "osd")) {
    $source = Join-Path $installedDirectory "$language.traineddata"
    $target = Join-Path $targetDirectory "$language.traineddata"
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Required Tesseract language file was not found: $source"
    }
    if ($Force -or -not (Test-Path -LiteralPath $target)) {
        Copy-Item -LiteralPath $source -Destination $target -Force
    }
}

$frenchTarget = Join-Path $targetDirectory "fra.traineddata"
if ($Force -or -not (Test-Path -LiteralPath $frenchTarget)) {
    $temporaryTarget = Join-Path $targetDirectory "fra.traineddata.download"
    try {
        Write-Host "Downloading official French OCR language data..." -ForegroundColor Cyan
        Invoke-WebRequest -Uri $frenchUrl -OutFile $temporaryTarget -UseBasicParsing
        if ((Get-Item -LiteralPath $temporaryTarget).Length -lt 1000000) {
            throw "The downloaded French language file is unexpectedly small."
        }
        Move-Item -LiteralPath $temporaryTarget -Destination $frenchTarget -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryTarget) {
            Remove-Item -LiteralPath $temporaryTarget -Force
        }
    }
}

Write-Host ""
Write-Host "OCR language files installed in: $targetDirectory" -ForegroundColor Green
& tesseract --tessdata-dir $targetDirectory --list-langs

