<#
.SYNOPSIS
    Desk Agent R1 Windows Environment & Connection Verification Wizard.
.DESCRIPTION
    Checks local Python environment, Codex CLI, Companion / Host ports,
    configuration validation, and tests communication with the robot bridge.
#>

param (
    [string]$CompanionHost = "127.0.0.1",
    [int]$CompanionPort = 8765,
    [string]$HostHost = "127.0.0.1",
    [int]$HostPort = 8088,
    [string]$RobotIp = "127.0.0.1",
    [int]$RobotTcpPort = 37020
)

$ErrorActionPreference = "Continue"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Desk Agent R1 - Setup & Connection Diagnostics Wizard   " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$allPass = $true

# 1. Check Python
Write-Host "`n[1/6] Checking Python runtime..." -ForegroundColor Yellow
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    $pyVer = & python --version 2>&1
    Write-Host "  [OK] Python found: $pyVer ($($py.Source))" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Python 3 not found in PATH." -ForegroundColor Red
    $allPass = $false
}

# 2. Check Codex CLI
Write-Host "`n[2/6] Checking Codex CLI..." -ForegroundColor Yellow
$codex = Get-Command codex -ErrorAction SilentlyContinue
if ($codex) {
    Write-Host "  [OK] Codex CLI found: $($codex.Source)" -ForegroundColor Green
} else {
    Write-Host "  [WARN] Codex CLI not found in PATH. Install Codex CLI for AI coding integration." -ForegroundColor Yellow
}

# 3. Check Companion Configuration
Write-Host "`n[3/6] Checking Codex Companion configuration..." -ForegroundColor Yellow
$companionConfig = "$PSScriptRoot\companion-config.json"
if (Test-Path $companionConfig) {
    $checkOut = & python -m companion.src.codex_companion --config "$companionConfig" --check-config 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [OK] Companion configuration validated: $checkOut" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] Companion configuration error: $checkOut" -ForegroundColor Red
        $allPass = $false
    }
} else {
    Write-Host "  [WARN] companion-config.json not found. Copying from example..." -ForegroundColor Yellow
    Copy-Item "$PSScriptRoot\companion-config.example.json" "$companionConfig"
    Write-Host "  [OK] Created $companionConfig from template." -ForegroundColor Green
}

# 4. Check Codex Companion HTTP endpoint
Write-Host "`n[4/6] Testing Codex Companion connection ($CompanionHost`:$CompanionPort)..." -ForegroundColor Yellow
try {
    $resp = Invoke-RestMethod -Uri "http://${CompanionHost}:${CompanionPort}/health" -TimeoutSec 3 -ErrorAction Stop
    Write-Host "  [OK] Companion is healthy: status=$($resp.status)" -ForegroundColor Green
} catch {
    Write-Host "  [INFO] Companion not currently running on port $CompanionPort (Start with: deploy\windows\start-companion.ps1)" -ForegroundColor Gray
}

# 5. Check Host Service HTTP endpoint
Write-Host "`n[5/6] Testing Host Service connection ($HostHost`:$HostPort)..." -ForegroundColor Yellow
try {
    $resp = Invoke-RestMethod -Uri "http://${HostHost}:${HostPort}/health" -TimeoutSec 3 -ErrorAction Stop
    Write-Host "  [OK] Host Service is healthy: status=$($resp.status)" -ForegroundColor Green
} catch {
    Write-Host "  [INFO] Host Service not currently running on port $HostPort (Start with: deploy\windows\run-host.ps1 or TaishanPi systemd)" -ForegroundColor Gray
}

# 6. Check Robot TCP Port
Write-Host "`n[6/6] Testing Robot SDK Bridge TCP connection ($RobotIp`:$RobotTcpPort)..." -ForegroundColor Yellow
try {
    $tcpClient = New-Object System.Net.Sockets.TcpClient
    $iar = $tcpClient.BeginConnect($RobotIp, $RobotTcpPort, $null, $null)
    $success = $iar.AsyncWaitHandle.WaitOne(2000, $false)
    if ($success -and $tcpClient.Connected) {
        Write-Host "  [OK] Successfully connected to Robot SDK Bridge on port $RobotTcpPort!" -ForegroundColor Green
        $tcpClient.EndConnect($iar)
        $tcpClient.Close()
    } else {
        Write-Host "  [INFO] Robot SDK Bridge not reachable at $RobotIp`:$RobotTcpPort (Ensure ESP32-S3 Watcher is powered on and connected to Wi-Fi)." -ForegroundColor Gray
    }
} catch {
    Write-Host "  [INFO] Robot SDK Bridge not reachable: $_" -ForegroundColor Gray
}

Write-Host "`n==========================================================" -ForegroundColor Cyan
if ($allPass) {
    Write-Host " Diagnostics complete. System is ready!" -ForegroundColor Green
    Write-Host " Quick Start Commands:" -ForegroundColor Yellow
    Write-Host "   Start Companion: powershell -File deploy\windows\start-companion.ps1" -ForegroundColor White
    Write-Host "   Start Host:      powershell -File deploy\windows\run-host.ps1" -ForegroundColor White
    Write-Host "   Register Auto:   powershell -File deploy\windows\register-autostart.ps1" -ForegroundColor White
} else {
    Write-Host " Diagnostics completed with warnings/errors. Review above output." -ForegroundColor Yellow
}
Write-Host "==========================================================" -ForegroundColor Cyan
