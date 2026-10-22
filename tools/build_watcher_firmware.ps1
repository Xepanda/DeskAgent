param(
    [string]$SdkRoot = "$env:USERPROFILE\TuyaOpenIDE\TuyaOpenSDK"
)
$ErrorActionPreference = 'Stop'

function Get-Sha256Bytes([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-', '') }
    finally { $sha.Dispose() }
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$firmwareRoot = Join-Path $projectRoot 'firmware'
# The IDE surface uses this local alias. A fresh clone must be buildable without
# copying firmware sources or relying on the previous developer's junction.
$surfaceRoot = Join-Path $projectRoot 'source'
$surfacePath = Join-Path $surfaceRoot 'embedded'
if (!(Test-Path -LiteralPath $surfacePath)) {
    New-Item -ItemType Directory -Path $surfaceRoot -Force | Out-Null
    New-Item -ItemType Junction -Path $surfacePath -Target $firmwareRoot | Out-Null
} else {
    $surfaceItem = Get-Item -LiteralPath $surfacePath
    if ($surfaceItem.LinkType -ne 'Junction' -or
        [IO.Path]::GetFullPath($surfaceItem.Target).TrimEnd('\') -ne $firmwareRoot.TrimEnd('\')) {
        throw "Embedded surface must be a junction to $firmwareRoot; existing content was preserved."
    }
}
if (!(Test-Path -LiteralPath (Join-Path $SdkRoot 'tos.py'))) { throw "TuyaOpen SDK not found: $SdkRoot" }

# ESP32 phase 2 reuses a machine-local generated sdkconfig. Its previous
# board's 16MB choice can otherwise survive the project's 32MB Tuya config.
# Change only this generated file, never the SDK's tracked board defaults.
$nativeConfig = Join-Path $SdkRoot 'platform\ESP32\tuya_open_sdk\sdkconfig'
$sdkPartitionCsv = Join-Path $SdkRoot 'platform\ESP32\tuya_open_sdk\partitions.csv'
$targetMarker = Join-Path $SdkRoot 'platform\ESP32\.target'
$appMarker = Join-Path $SdkRoot 'platform\ESP32\.app'
if (!(Test-Path -LiteralPath $nativeConfig) -or !(Test-Path -LiteralPath $targetMarker) -or !(Test-Path -LiteralPath $appMarker)) {
    throw 'ESP32 generated sdkconfig/target/app markers are missing; run the existing TuyaOpen target setup first.'
}
if ([IO.File]::ReadAllText($targetMarker).Trim() -ne 'esp32s3' -or
    [IO.File]::ReadAllText($appMarker).Trim() -ne 'desk_agent_firmware') {
    throw 'SDK target/app markers do not match this project (esp32s3 / desk_agent_firmware).'
}
$sdkConfigBuild = Join-Path $SdkRoot 'platform\ESP32\tuya_open_sdk\build\config\sdkconfig.h'
if (!(Test-Path -LiteralPath $sdkConfigBuild)) { throw 'Generated ESP-IDF sdkconfig.h is missing; refusing to trigger SDK target setup.' }
$configBytes = [IO.File]::ReadAllBytes($nativeConfig)
$partBytes = if (Test-Path -LiteralPath $sdkPartitionCsv) { [IO.File]::ReadAllBytes($sdkPartitionCsv) } else { $null }
$configTime = (Get-Item -LiteralPath $nativeConfig).LastWriteTimeUtc
$partTime = if ($null -ne $partBytes) { (Get-Item -LiteralPath $sdkPartitionCsv).LastWriteTimeUtc } else { $null }

$runId = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$archiveRoot = Join-Path $projectRoot ".target_logging\build-partitions-$runId"
New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
$sdkConfigProvenancePath = Join-Path $archiveRoot 'sdkconfig-provenance.json'

# Recompute the TuyaOpen app configuration before its generator is invoked.
# Save exact generated-cache bytes so a failed build can put machine state back.
$inputs = @((Join-Path $firmwareRoot 'app_default.config'), (Join-Path $firmwareRoot 'Kconfig'))
$inputs += Get-ChildItem -LiteralPath (Join-Path $firmwareRoot 'third_party\tclaw') -Recurse -File -Filter '*Kconfig*' |
    Sort-Object FullName | ForEach-Object FullName
$fingerprint = ($inputs | ForEach-Object { (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash }) -join ':'
$cacheRoot = Join-Path $firmwareRoot '.build\cache'
New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
$stamp = Join-Path $cacheRoot 'desk-config-input.sha256'
$generatedPaths = @(
    (Join-Path $cacheRoot 'using.config'),
    (Join-Path $cacheRoot 'using.cmake'),
    (Join-Path $firmwareRoot '.build\include\tuya_kconfig.h'),
    $stamp
)
$generatedBackup = @{}
foreach ($path in $generatedPaths) {
    if (Test-Path -LiteralPath $path) {
        $item = Get-Item -LiteralPath $path
        $generatedBackup[$path] = [pscustomobject]@{
            Exists = $true; Bytes = [IO.File]::ReadAllBytes($path); LastWriteTimeUtc = $item.LastWriteTimeUtc
        }
    } else {
        $generatedBackup[$path] = [pscustomobject]@{ Exists = $false; Bytes = $null; LastWriteTimeUtc = $null }
    }
}
$oldFingerprint = if (Test-Path -LiteralPath $stamp) { [IO.File]::ReadAllText($stamp) } else { $null }
$fingerprintChanged = $oldFingerprint -ne $fingerprint
$buildSucceeded = $false
try {
    if ($fingerprintChanged) {
        foreach ($path in $generatedPaths | Where-Object { $_ -ne $stamp }) {
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
        }
        [IO.File]::WriteAllText($stamp, $fingerprint)
    }
    $nativeText = [Text.Encoding]::UTF8.GetString($configBytes)
    $nativeNext = [regex]::Replace($nativeText,
        '(?m)^(?:# )?CONFIG_ESPTOOLPY_FLASHSIZE(?:_(?:1|2|4|8|16|32|64|128)MB)?(?:=.*| is not set)\r?\n?', '')
    $nativeNext = [regex]::Replace($nativeNext,
        '(?m)^(?:# )?CONFIG_PARTITION_TABLE_(?:CUSTOM_FILENAME|FILENAME)(?:=.*| is not set)\r?\n?', '')
    $partitionPath = (Join-Path $firmwareRoot 'partitions.csv').Replace('\', '/')
    $nativeNext += "`nCONFIG_ESPTOOLPY_FLASHSIZE_32MB=y`nCONFIG_ESPTOOLPY_FLASHSIZE=`"32MB`"`n"
    $nativeNext += "CONFIG_PARTITION_TABLE_CUSTOM=y`nCONFIG_PARTITION_TABLE_CUSTOM_FILENAME=`"$partitionPath`"`nCONFIG_PARTITION_TABLE_FILENAME=`"$partitionPath`"`n"
    [IO.File]::WriteAllText($nativeConfig, $nativeNext, [Text.UTF8Encoding]::new($false))
    $temporarySdkConfigSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $nativeConfig).Hash
    [ordered]@{
        sdkconfig_path = $nativeConfig
        sdkconfig_sha256_before = Get-Sha256Bytes $configBytes
        sdkconfig_mtime_before_utc = $configTime.ToString('o')
        temporary_sdkconfig_sha256 = $temporarySdkConfigSha
        temporary_overrides = [ordered]@{
            CONFIG_ESPTOOLPY_FLASHSIZE_32MB = 'y'
            CONFIG_ESPTOOLPY_FLASHSIZE = '32MB'
            CONFIG_PARTITION_TABLE_CUSTOM = 'y'
            CONFIG_PARTITION_TABLE_CUSTOM_FILENAME = $partitionPath
            CONFIG_PARTITION_TABLE_FILENAME = $partitionPath
        }
        restoration = 'pending'
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $sdkConfigProvenancePath -Encoding utf8

    # SDK build_example.py copies a vendor table into its project directory;
    # the absolute sdkconfig values above make that filename irrelevant. Run
    # TuyaOpen's config/CMake/Ninja phases through the local bridge and archive
    # only the validated build outputs under .target_logging.
    $python = Join-Path $SdkRoot '.venv\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $python)) { throw "TuyaOpen virtualenv Python not found: $python" }
    $idfRoot = Join-Path $SdkRoot 'platform\ESP32\esp-idf'
    $idfExport = Join-Path $idfRoot 'export.bat'
    if (!(Test-Path -LiteralPath $idfExport)) { throw "ESP-IDF export script not found: $idfExport" }
    # export.bat selects its Python environment from the `python.exe` found on
    # PATH. Prefer an existing IDF 5.4 environment whose `rich` dependency is
    # present under this SDK's own .espressif root; then execute the bridge
    # in the same CMD process so IDF_PATH, IDF_PYTHON_ENV_PATH and tool PATH are
    # inherited by TuyaOpen's configure/build subprocesses. This changes only
    # this child process environment; no machine-level environment is written.
    $idfToolsRoot = Join-Path $SdkRoot 'platform\ESP32\.espressif'
    $idfPythonEnvRoot = Join-Path $idfToolsRoot 'python_env'
    $idfPythonCandidates = Get-ChildItem -LiteralPath $idfPythonEnvRoot -Directory -Filter 'idf5.4_py*_env' -ErrorAction SilentlyContinue |
        Sort-Object { if ($_.Name -match 'py3\.12') { 0 } else { 1 } }, Name
    $idfPythonScripts = $null
    foreach ($candidate in $idfPythonCandidates) {
        $candidatePython = Join-Path $candidate.FullName 'Scripts\python.exe'
        $versionFile = Join-Path $candidate.FullName 'idf_version.txt'
        if (!(Test-Path -LiteralPath $candidatePython) -or !(Test-Path -LiteralPath $versionFile) -or
            [IO.File]::ReadAllText($versionFile).Trim() -ne '5.4') { continue }
        $oldErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $candidatePython -c 'import rich' 2>$null | Out-Null
            $candidateExitCode = $LASTEXITCODE
        } finally { $ErrorActionPreference = $oldErrorActionPreference }
        if ($candidateExitCode -eq 0) { $idfPythonScripts = Join-Path $candidate.FullName 'Scripts'; break }
    }
    if (!$idfPythonScripts) {
        throw "No existing ESP-IDF 5.4 Python environment with required 'rich' module was found under $idfPythonEnvRoot; no environment was installed or modified."
    }
    $bridge = Join-Path $projectRoot 'tools\build_watcher_tuyaopen.py'
    $buildLog = Join-Path $archiveRoot 'build-console.log'
    $cmdLine = 'set "IDF_TOOLS_PATH=' + $idfToolsRoot + '" && set "PATH=' + $idfPythonScripts + ';%PATH%" && call "' + $idfExport +
        '" && where idf.py && "' + $python + '" "' + $bridge + '" "' + $SdkRoot +
        '" "' + $surfacePath + '" "' + $archiveRoot + '"'
    Start-Transcript -LiteralPath $buildLog -Force | Out-Null
    try {
        & $env:ComSpec /d /s /c ('"' + $cmdLine + '"')
        $buildExitCode = $LASTEXITCODE
    } finally { Stop-Transcript | Out-Null }
    if (Test-Path -LiteralPath $buildLog) { Get-Content -LiteralPath $buildLog -Tail 100 }
    $LASTEXITCODE = $buildExitCode
    if ($buildExitCode -ne 0) { throw "Verified target build failed with exit code $buildExitCode; full output: $buildLog" }
    $buildSucceeded = $true
} finally {
    [IO.File]::WriteAllBytes($nativeConfig, $configBytes)
    (Get-Item -LiteralPath $nativeConfig).LastWriteTimeUtc = $configTime
    if ($null -ne $partBytes) {
        [IO.File]::WriteAllBytes($sdkPartitionCsv, $partBytes)
        (Get-Item -LiteralPath $sdkPartitionCsv).LastWriteTimeUtc = $partTime
    }
    if (!$buildSucceeded -and $fingerprintChanged) {
        foreach ($path in $generatedPaths) {
            $saved = $generatedBackup[$path]
            if ($saved.Exists) {
                $parent = Split-Path -Parent $path
                New-Item -ItemType Directory -Path $parent -Force | Out-Null
                [IO.File]::WriteAllBytes($path, $saved.Bytes)
                (Get-Item -LiteralPath $path).LastWriteTimeUtc = $saved.LastWriteTimeUtc
            } elseif (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path
            }
        }
    }
    $restoreEvidence = [ordered]@{
        sdkconfig_sha256_before = Get-Sha256Bytes $configBytes
        sdkconfig_sha256_after = (Get-FileHash -Algorithm SHA256 -LiteralPath $nativeConfig).Hash
        sdkconfig_mtime_restored = ((Get-Item -LiteralPath $nativeConfig).LastWriteTimeUtc -eq $configTime)
        sdk_partitions_sha256_before = if ($null -ne $partBytes) { Get-Sha256Bytes $partBytes } else { $null }
        sdk_partitions_sha256_after = if (Test-Path -LiteralPath $sdkPartitionCsv) { (Get-FileHash -Algorithm SHA256 -LiteralPath $sdkPartitionCsv).Hash } else { $null }
        sdk_partitions_mtime_restored = if ($null -ne $partBytes) { ((Get-Item -LiteralPath $sdkPartitionCsv).LastWriteTimeUtc -eq $partTime) } else { $true }
        generated_cache_restored_after_failure = (!$fingerprintChanged -or $buildSucceeded -or ($generatedPaths | Where-Object {
            $saved = $generatedBackup[$_]
            if ($saved.Exists) { !(Test-Path -LiteralPath $_) -or (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash -ne (Get-Sha256Bytes $saved.Bytes) }
            else { Test-Path -LiteralPath $_ }
        }).Count -eq 0)
    }
    New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
    $restoreEvidence | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $archiveRoot 'machine-state-restoration.json') -Encoding utf8
    $sdkConfigProvenance = Get-Content -LiteralPath $sdkConfigProvenancePath -Raw | ConvertFrom-Json
    $sdkConfigProvenance.restoration = [ordered]@{
        sdkconfig_sha256_after = $restoreEvidence.sdkconfig_sha256_after
        sdkconfig_mtime_restored = $restoreEvidence.sdkconfig_mtime_restored
        byte_content_restored = ($restoreEvidence.sdkconfig_sha256_before -eq $restoreEvidence.sdkconfig_sha256_after)
    }
    $sdkConfigProvenance | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $sdkConfigProvenancePath -Encoding utf8
}
