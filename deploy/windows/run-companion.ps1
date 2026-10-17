# PowerShell runner for Desk Agent R1 Windows Codex Companion
#
# Startup security (interfaces.md 6.5 / WP5): this runner never copies the committed example
# config into place. The example is loopback-only with no usable token and is a template, not a
# runnable configuration. The Companion refuses to start when a non-loopback host is configured
# without a strong auth_token, and this script exits non-zero in that case.
param(
    [string]$ConfigFile = "$PSScriptRoot\companion-config.json"
)

$ErrorActionPreference = "Stop"
# Govern failures by the explicit $LASTEXITCODE checks below (PowerShell 7.3+ only; harmless on 5.1).
$PSNativeCommandUseErrorActionPreference = $false

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " Desk Agent R1 Windows Codex Companion   " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] Python 3 is required but was not found in PATH." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $ConfigFile)) {
    Write-Host "[ERROR] Configuration file $ConfigFile not found. Refusing to start." -ForegroundColor Red
    Write-Host "Create it explicitly from the template:" -ForegroundColor Yellow
    Write-Host "  Copy-Item `"$PSScriptRoot\companion-config.example.json`" `"$ConfigFile`""
    Write-Host "Then set host/auth_token. Non-loopback hosts (e.g. 0.0.0.0) require a strong auth_token"
    Write-Host "(>= 16 chars); placeholder values such as REPLACE_WITH_... are rejected."
    exit 1
}

# Pre-flight validation: same path as the library and CLI entry points (interfaces.md 6.5).
python -m companion.src.codex_companion --config "$ConfigFile" --check-config
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[ERROR] Companion configuration rejected. Refusing to start an exposed or unauthenticated service." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "Starting Codex Companion Service on port 8765..." -ForegroundColor Green
python -m companion.src.codex_companion --config $ConfigFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Codex Companion exited with code $LASTEXITCODE." -ForegroundColor Red
    exit $LASTEXITCODE
}
