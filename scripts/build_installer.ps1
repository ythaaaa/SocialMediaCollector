param(
    [string]$IsccPath = ""
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $projectRoot

function Resolve-Iscc {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        if (Test-Path -LiteralPath $RequestedPath -PathType Leaf) {
            return (Resolve-Path -LiteralPath $RequestedPath).Path
        }
        throw "Requested ISCC.exe was not found: $RequestedPath"
    }

    $candidates = @()
    $command = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    foreach ($key in @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )) {
        $entries = Get-ItemProperty $key -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -like 'Inno Setup*' -and $_.InstallLocation }
        foreach ($entry in $entries) {
            $candidates += Join-Path $entry.InstallLocation 'ISCC.exe'
        }
    }
    $candidates += @(
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    )
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw 'Inno Setup 6 ISCC.exe was not found. Install Inno Setup 6 first.'
}

$iscc = Resolve-Iscc $IsccPath
$isccVersion = $null
foreach ($key in @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
)) {
    $entries = Get-ItemProperty $key -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like 'Inno Setup*' -and $_.DisplayVersion -and $_.InstallLocation }
    foreach ($entry in $entries) {
        $registeredIscc = Join-Path $entry.InstallLocation 'ISCC.exe'
        if ((Resolve-Path -LiteralPath $registeredIscc -ErrorAction SilentlyContinue).Path -eq $iscc) {
            $isccVersion = $entry.DisplayVersion
            break
        }
    }
    if ($isccVersion) { break }
}
if (-not $isccVersion) {
    $isccVersion = (Get-Item -LiteralPath $iscc).VersionInfo.ProductVersion
}
if (-not $isccVersion.StartsWith('6.')) {
    throw "Inno Setup 6 is required. Current version: $isccVersion"
}

$distRoot = Join-Path $projectRoot 'dist\SocialMediaCollector'
$distExe = Join-Path $distRoot 'SocialMediaCollector.exe'
$bundledFfmpeg = Join-Path $distRoot '_internal\vendor\ffmpeg\ffmpeg.exe'
$ffmpegDir = Split-Path $bundledFfmpeg
$requiredDlls = @(
    'avcodec-62.dll', 'avdevice-62.dll', 'avfilter-11.dll',
    'avformat-62.dll', 'avutil-60.dll', 'swresample-6.dll', 'swscale-9.dll'
)
$requiredFiles = @(
    $distExe,
    $bundledFfmpeg,
    (Join-Path $projectRoot 'assets\app_icon.ico'),
    (Join-Path $projectRoot 'THIRD_PARTY_NOTICES.md'),
    (Join-Path $projectRoot 'third_party\ffmpeg\LICENSE.txt'),
    (Join-Path $projectRoot 'third_party\ffmpeg\NOTICE.txt'),
    (Join-Path $projectRoot 'third_party\ffmpeg\SOURCE_INFO.txt'),
    (Join-Path $projectRoot 'third_party\ffmpeg\BUILD_INFO.txt')
)
foreach ($required in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required installer input is missing: $required"
    }
}
foreach ($dll in $requiredDlls) {
    $path = Join-Path $ffmpegDir $dll
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Bundled FFmpeg DLL is missing: $path"
    }
}

$forbidden = @(Get-ChildItem -LiteralPath $distRoot -Recurse -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '(?i)(browser-data|cookie|local.?storage|\.venv|\\tests?($|\\)|\.part|\.env|secret|password|token)' })
if ($forbidden.Count -gt 0) {
    $names = ($forbidden | Select-Object -ExpandProperty FullName) -join "`n"
    throw "dist contains paths that must not enter the installer:`n$names"
}

$ffmpegInfo = (& $bundledFfmpeg -version 2>&1 | Out-String)
if ($ffmpegInfo -notmatch 'n8\.1\.2-44-g7c533d0f86-20260823') {
    throw 'The FFmpeg in dist is not the specified LGPL build.'
}
if ($ffmpegInfo -match '--enable-gpl|--enable-nonfree') {
    throw 'The FFmpeg in dist contains GPL or nonfree build flags.'
}

$iss = Join-Path $projectRoot 'installer\SocialMediaCollector.iss'
$output = Join-Path $projectRoot 'release\SocialMediaCollector-Setup-v2.0.0.exe'
if (-not (Test-Path -LiteralPath $iss -PathType Leaf)) {
    throw "Inno Setup source file is missing: $iss"
}
New-Item -ItemType Directory -Force -Path (Split-Path $output) | Out-Null
if (Test-Path -LiteralPath $output) {
    Remove-Item -LiteralPath $output -Force
}

Write-Host "Using Inno Setup ${isccVersion}: $iscc"
& $iscc /Qp $iss
if ($LASTEXITCODE -ne 0) {
    throw "ISCC build failed with exit code: $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $output -PathType Leaf)) {
    throw "Build completed but installer was not found: $output"
}
$size = (Get-Item -LiteralPath $output).Length
Write-Host "Complete: $output ($size bytes)"
