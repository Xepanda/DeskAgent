<#
.SYNOPSIS
    Starts Desk Agent R1 Windows Codex Companion Daemon.
.DESCRIPTION
    Launches the Codex Companion HTTP RPC server managing local Codex CLI / app-server.
#>

param (
    [string]$ConfigPath = "$PSScriptRoot\companion-config.example.json",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Desk Agent R1 - Windows Codex Companion Daemon          " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# Check if Python is available
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python 3 is required but was not found in PATH."
    exit 1
}

# Check if Codex binary is available
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Warning "Codex executable not found in PATH. Ensure Codex CLI is installed."
}

Write-Host "[INFO] Loading configuration from: $ConfigPath" -ForegroundColor Green
Write-Host "[INFO] Starting companion on port $Port..." -ForegroundColor Green

python -m companion.src.codex_companion --config "$ConfigPath" --port $Port
