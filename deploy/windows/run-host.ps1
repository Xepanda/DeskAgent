<#
.SYNOPSIS
    Starts Desk Agent R1 Host Application Daemon on Windows.
.DESCRIPTION
    Launches host.desk_agent.main with configuration check.
#>

param (
    [string]$ConfigPath = "$PSScriptRoot\..\edge\config.json",
    [int]$Port = 8088
)

$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Desk Agent R1 - Host Application Daemon (Windows)       " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] Python 3 was not found in PATH." -ForegroundColor Red
    exit 1
}

$repoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$env:PYTHONPATH = $repoRoot

if (-not (Test-Path $ConfigPath)) {
    $exampleConfig = "$PSScriptRoot\..\edge\config.example.json"
    if (Test-Path $exampleConfig) {
        Write-Host "[WARN] Config file not found at $ConfigPath. Initializing from template..." -ForegroundColor Yellow
        Copy-Item $exampleConfig $ConfigPath
    } else {
        Write-Host "[ERROR] Neither $ConfigPath nor $exampleConfig was found." -ForegroundColor Red
        exit 1
    }
}

Write-Host "[INFO] Starting Host Service with config: $ConfigPath" -ForegroundColor Green
Set-Location $repoRoot
python -m host.desk_agent.main --config "$ConfigPath" --port $Port
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Host Service exited with code $LASTEXITCODE." -ForegroundColor Red
    exit $LASTEXITCODE
}
