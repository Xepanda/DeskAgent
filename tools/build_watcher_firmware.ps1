param(
    [string]$SdkRoot = "$env:USERPROFILE\TuyaOpenIDE\TuyaOpenSDK"
)
$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runner = Join-Path $projectRoot 'tools\verification\build_isolated_firmware.py'
$python = (Get-Command python -ErrorAction Stop).Source
$archiveRoot = Join-Path $projectRoot 'firmware\.target_logging'
New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
$runId = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$archive = Join-Path $archiveRoot "isolated-firmware-$runId"

# The Python runner stages all writable state under this new archive. This
# wrapper deliberately does not inspect SDK generated configuration, build
# outputs, machine-local markers, or credentials.
& $python $runner --repo $projectRoot --sdk $SdkRoot --archive $archive
if ($LASTEXITCODE -ne 0) {
    throw "Isolated firmware build refused or failed with exit code $LASTEXITCODE."
}
