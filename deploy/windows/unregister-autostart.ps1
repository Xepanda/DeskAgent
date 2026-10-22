<#
.SYNOPSIS
    Removes DeskAgent Windows autostart tasks.
.DESCRIPTION
    Unregisters Scheduled Tasks and Registry Run keys created by register-autostart.ps1.
#>

$ErrorActionPreference = "SilentlyContinue"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Desk Agent R1 - Remove Windows Autostart                " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$tasks = @("DeskAgent_CodexCompanion", "DeskAgent_Host")
$regKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

foreach ($taskName in $tasks) {
    # 1. Try Scheduled Task
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false | Out-Null
        Write-Host "[INFO] Unregistered Scheduled Task: $taskName" -ForegroundColor Green
    }

    # 2. Try Registry Run Key
    $regVal = Get-ItemProperty -Path $regKey -Name $taskName -ErrorAction SilentlyContinue
    if ($regVal) {
        Remove-ItemProperty -Path $regKey -Name $taskName -ErrorAction SilentlyContinue
        Write-Host "[INFO] Removed Registry Run Entry: $taskName" -ForegroundColor Green
    }
}

Write-Host "[SUCCESS] Autostart entries cleaned up." -ForegroundColor Green
