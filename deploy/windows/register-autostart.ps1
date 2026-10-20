<#
.SYNOPSIS
    Registers DeskAgent Windows Codex Companion (and optionally Host) as an autostart task.
.DESCRIPTION
    Creates a user-level Windows Scheduled Task triggered at logon to launch
    the Codex Companion daemon automatically in the background.
    Safe for non-admin accounts.
#>

param (
    [switch]$IncludeHost,
    [string]$CompanionConfig = "$PSScriptRoot\companion-config.json",
    [string]$HostConfig = "$PSScriptRoot\..\edge\config.json"
)

$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Desk Agent R1 - Windows Autostart Registration          " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Verify Python
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] Python 3 was not found in PATH. Please install Python >= 3.10." -ForegroundColor Red
    exit 1
}

# 2. Check Companion Config
if (-not (Test-Path $CompanionConfig)) {
    Write-Host "[WARN] Companion config ($CompanionConfig) not found." -ForegroundColor Yellow
    Write-Host "       Creating from template: companion-config.example.json" -ForegroundColor Yellow
    Copy-Item "$PSScriptRoot\companion-config.example.json" "$CompanionConfig"
}

# 3. Register Codex Companion Scheduled Task
$companionTaskName = "DeskAgent_CodexCompanion"
$startCompanionScript = (Resolve-Path "$PSScriptRoot\start-companion.ps1").Path

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Minimized -File `"$startCompanionScript`" -ConfigPath `"$CompanionConfig`"" `
    -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 0)

try {
    # Check if task already exists
    $existing = Get-ScheduledTask -TaskName $companionTaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "[INFO] Updating existing task: $companionTaskName" -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $companionTaskName -Confirm:$false | Out-Null
    }

    Register-ScheduledTask -TaskName $companionTaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "Desk Agent R1 Windows Codex Companion Daemon" | Out-Null

    Write-Host "[SUCCESS] Registered '$companionTaskName' to start on logon." -ForegroundColor Green
} catch {
    Write-Host "[ERROR] Failed to register Scheduled Task: $_" -ForegroundColor Red
    Write-Host "        Falling back to Registry Run Key (HKCU:\Software\Microsoft\Windows\CurrentVersion\Run)..." -ForegroundColor Yellow
    $regKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    $regCmd = "powershell.exe -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$startCompanionScript`" -ConfigPath `"$CompanionConfig`""
    Set-ItemProperty -Path $regKey -Name $companionTaskName -Value $regCmd
    Write-Host "[SUCCESS] Registered '$companionTaskName' via Registry Run Key." -ForegroundColor Green
}

# 4. Optionally register Host service if requested
if ($IncludeHost) {
    $hostTaskName = "DeskAgent_Host"
    $runHostScript = (Resolve-Path "$PSScriptRoot\run-host.ps1").Path
    $hostAction = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-ExecutionPolicy Bypass -WindowStyle Minimized -File `"$runHostScript`" -ConfigPath `"$HostConfig`"" `
        -WorkingDirectory $PSScriptRoot

    try {
        $existingHost = Get-ScheduledTask -TaskName $hostTaskName -ErrorAction SilentlyContinue
        if ($existingHost) {
            Unregister-ScheduledTask -TaskName $hostTaskName -Confirm:$false | Out-Null
        }
        Register-ScheduledTask -TaskName $hostTaskName `
            -Action $hostAction `
            -Trigger $trigger `
            -Settings $settings `
            -Description "Desk Agent R1 Host Application Daemon" | Out-Null
        Write-Host "[SUCCESS] Registered '$hostTaskName' to start on logon." -ForegroundColor Green
    } catch {
        $regKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
        $regCmd = "powershell.exe -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$runHostScript`" -ConfigPath `"$HostConfig`""
        Set-ItemProperty -Path $regKey -Name $hostTaskName -Value $regCmd
        Write-Host "[SUCCESS] Registered '$hostTaskName' via Registry Run Key." -ForegroundColor Green
    }
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Autostart registration complete." -ForegroundColor Green
Write-Host " To unregister: powershell -File deploy\windows\unregister-autostart.ps1" -ForegroundColor Gray
Write-Host "==========================================================" -ForegroundColor Cyan
