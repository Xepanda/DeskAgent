<#
.SYNOPSIS
    Starts Desk Agent R1 Windows Codex Companion Daemon.
.DESCRIPTION
    Launches the Codex Companion HTTP RPC server managing local Codex CLI / app-server.

    Startup security (interfaces.md 6.5 / WP5): the Companion binds 127.0.0.1 by default and
    refuses to start when the configured host is non-loopback (e.g. 0.0.0.0) without an
    explicitly configured strong auth_token. This script never copies the committed example
    config over a live config, and it exits non-zero when the binding is refused.
#>

param (
    [string]$ConfigPath = "$PSScriptRoot\companion-config.json",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
# Govern failures by the explicit $LASTEXITCODE checks below (PowerShell 7.3+ only; harmless on 5.1).
$PSNativeCommandUseErrorActionPreference = $false

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Desk Agent R1 - Windows Codex Companion Daemon          " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# Check if Python is available
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] Python 3 is required but was not found in PATH." -ForegroundColor Red
    exit 1
}

# Check if Codex binary is available
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Warning "Codex executable not found in PATH. Ensure Codex CLI is installed."
}

# The committed example config is loopback-only and carries no usable token; it is a template,
# not a runnable configuration. Never silently launch from it.
if (-not (Test-Path $ConfigPath)) {
    Write-Host "[ERROR] Companion configuration not found: $ConfigPath" -ForegroundColor Red
    Write-Host "Create it explicitly, for example:" -ForegroundColor Yellow
    Write-Host "  Copy-Item `"$PSScriptRoot\companion-config.example.json`" `"$PSScriptRoot\companion-config.json`""
    Write-Host "Then review it. If host is non-loopback (e.g. 0.0.0.0) you MUST set a strong auth_token"
    Write-Host "(>= 16 chars) or the Companion will refuse to start."
    exit 1
}

Write-Host "[INFO] Loading configuration from: $ConfigPath" -ForegroundColor Green

# Pre-flight: the same validation path as the library and CLI entry points (interfaces.md 6.5).
# Exits 0 when the binding is safe, 2 when it must be refused.
python -m companion.src.codex_companion --config "$ConfigPath" --port $Port --check-config
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[ERROR] Companion configuration rejected. Refusing to start an exposed or unauthenticated service." -ForegroundColor Red
    Write-Host "        Fix host/auth_token in $ConfigPath (see the message above)." -ForegroundColor Yellow
    exit $LASTEXITCODE
}

Write-Host "[INFO] Starting companion on port $Port..." -ForegroundColor Green
python -m companion.src.codex_companion --config "$ConfigPath" --port $Port
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Codex Companion exited with code $LASTEXITCODE." -ForegroundColor Red
    exit $LASTEXITCODE
}
