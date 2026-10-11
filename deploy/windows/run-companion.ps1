# PowerShell runner for Desk Agent R1 Windows Codex Companion
param(
    [string]$ConfigFile = "$PSScriptRoot\companion-config.json"
)

$ErrorActionPreference = "Stop"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " Desk Agent R1 Windows Codex Companion   " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

if (-not (Test-Path $ConfigFile)) {
    Write-Warning "Configuration file $ConfigFile not found. Copying companion-config.example.json..."
    Copy-Item "$PSScriptRoot\companion-config.example.json" $ConfigFile
}

Write-Host "Starting Codex Companion Service on port 8765..." -ForegroundColor Green
python -m companion.src.codex_companion --config $ConfigFile
