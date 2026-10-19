param(
    [string]$SdkRoot = "$env:USERPROFILE\TuyaOpenIDE\TuyaOpenSDK"
)
$ErrorActionPreference = 'Stop'
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
Get-Command tuyaopen-cli -ErrorAction Stop | Out-Null
if (!(Test-Path -LiteralPath (Join-Path $SdkRoot 'tos.py'))) { throw "TuyaOpen SDK not found: $SdkRoot" }

# ESP32 phase 2 reuses a machine-local generated sdkconfig. Its previous
# board's 16MB choice can otherwise survive the project's 32MB Tuya config.
# Change only this generated file, never the SDK's tracked board defaults.
$nativeConfig = Join-Path $SdkRoot 'platform\ESP32\tuya_open_sdk\sdkconfig'
if (Test-Path -LiteralPath $nativeConfig) {
    $nativeText = [IO.File]::ReadAllText($nativeConfig)
    $nativeNext = [regex]::Replace($nativeText,
        '(?m)^(?:# )?CONFIG_ESPTOOLPY_FLASHSIZE(?:_(?:1|2|4|8|16|32|64|128)MB)?(?:=.*| is not set)\r?\n?', '')
    $nativeNext += "`nCONFIG_ESPTOOLPY_FLASHSIZE_32MB=y`nCONFIG_ESPTOOLPY_FLASHSIZE=`"32MB`"`n"
    if ($nativeText -notmatch '(?m)^CONFIG_ESPTOOLPY_FLASHSIZE_32MB=y\r?$' -or
        $nativeText -notmatch '(?m)^CONFIG_ESPTOOLPY_FLASHSIZE="32MB"\r?$') {
        [IO.File]::WriteAllText($nativeConfig, $nativeNext)
    }
}

# SDK's config generator keeps existing derived CMake/header files. Invalidate
# only project-generated config artifacts when the actual Kconfig inputs change.
$inputs = @((Join-Path $firmwareRoot 'app_default.config'), (Join-Path $firmwareRoot 'Kconfig'))
$inputs += Get-ChildItem -LiteralPath (Join-Path $firmwareRoot 'third_party\tclaw') -Recurse -File -Filter '*Kconfig*' |
    Sort-Object FullName | ForEach-Object FullName
$fingerprint = ($inputs | ForEach-Object { (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash }) -join ':'
$cacheRoot = Join-Path $firmwareRoot '.build\cache'
New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
$stamp = Join-Path $cacheRoot 'desk-config-input.sha256'
if (!(Test-Path -LiteralPath $stamp) -or [IO.File]::ReadAllText($stamp) -ne $fingerprint) {
    foreach ($relative in @('.build\cache\using.config', '.build\cache\using.cmake', '.build\include\tuya_kconfig.h')) {
        $generated = Join-Path $firmwareRoot $relative
        if (Test-Path -LiteralPath $generated) { Remove-Item -LiteralPath $generated }
    }
    [IO.File]::WriteAllText($stamp, $fingerprint)
}
& tuyaopen-cli firmware build --project-root $projectRoot --sdk-root $SdkRoot --timeout 0 --stream --json
exit $LASTEXITCODE
