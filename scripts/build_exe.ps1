param(
    [string]$BuildRoot = '',
    [string]$DistRoot = '',
    [string]$GalleryDlHelperRelease = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $projectRoot

if (-not $BuildRoot) { $BuildRoot = Join-Path $projectRoot 'build' }
if (-not $DistRoot) { $DistRoot = Join-Path $projectRoot 'dist' }
$BuildRoot = [IO.Path]::GetFullPath($BuildRoot)
$DistRoot = [IO.Path]::GetFullPath($DistRoot)
foreach ($target in @($BuildRoot, $DistRoot)) {
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$uiResources = Join-Path $projectRoot 'src\ui\resources'
$assets = Join-Path $projectRoot 'assets'
$vendorRuntime = Join-Path $projectRoot 'vendor\ffmpeg'
$galleryDlWorker = Join-Path $projectRoot 'src\adapters\gallerydl_worker.py'
$instagramCookieBridgeWorker = Join-Path $projectRoot 'src\adapters\instagram_cookie_bridge_worker.py'
$ffmpeg = Join-Path $projectRoot 'vendor\ffmpeg\ffmpeg.exe'
$license = Join-Path $projectRoot 'vendor\ffmpeg\LICENSE.txt'
$systemIcu = Join-Path $env:SystemRoot 'System32\icuuc.dll'
$deno = Join-Path $env:USERPROFILE '.deno\bin\deno.exe'
foreach ($required in @($python, $ffmpeg, $license, $systemIcu, $deno)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "缺少发布资源：$required"
    }
}
$runtimeDlls = @(Get-ChildItem -LiteralPath $vendorRuntime -Filter '*.dll' -File)
if ($runtimeDlls.Count -eq 0) {
    throw "缺少 FFmpeg shared runtime DLL：$vendorRuntime"
}
$helperRelease = $null
if ($GalleryDlHelperRelease) {
    $helperRelease = [IO.Path]::GetFullPath($GalleryDlHelperRelease)
    $helperExe = Join-Path $helperRelease 'instagram-gallerydl-helper.exe'
    $helperInternal = Join-Path $helperRelease '_internal'
    if (-not (Test-Path -LiteralPath $helperExe -PathType Leaf) -or -not (Test-Path -LiteralPath $helperInternal -PathType Container)) {
        throw "gallery-dl helper onedir 不完整：$helperRelease"
    }
}

$pyInstallerArgs = @(
    '--noconfirm',
    '--clean',
    '--windowed',
    '--name', 'SocialMediaCollector',
    '--paths', '.',
    '--workpath', $BuildRoot,
    '--distpath', $DistRoot,
    '--specpath', $BuildRoot,
    '--add-data', "$uiResources;src\ui\resources",
    '--add-data', "$assets;assets",
    '--add-data', "$galleryDlWorker;src\adapters",
    '--add-data', "$instagramCookieBridgeWorker;src\adapters",
    '--add-data', "$license;vendor\ffmpeg",
    '--add-binary', "$ffmpeg;vendor\ffmpeg",
    '--icon', (Join-Path $assets 'app_icon.ico'),
    '--version-file', (Join-Path $projectRoot 'installer\version_info.txt'),
    '--collect-all', 'yt_dlp',
    '--collect-all', 'yt_dlp_ejs',
    '--add-binary', "$deno;tools\deno",
    'app.py'
)
if ($helperRelease) {
    $pyInstallerArgs += @('--add-data', "$helperRelease;helpers\instagram-gallerydl-helper")
}
foreach ($dll in $runtimeDlls) {
    $pyInstallerArgs += @('--add-binary', "$($dll.FullName);vendor\ffmpeg")
}

& $python -m PyInstaller @pyInstallerArgs

$release = Join-Path $DistRoot 'SocialMediaCollector'
$exe = Join-Path $release 'SocialMediaCollector.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "构建完成但未找到 EXE：$exe"
}
# Qt6Core needs the Windows ICU build with its unsuffixed exports. PyInstaller
# can otherwise pick an incompatible ICU from another runtime on PATH.
Copy-Item -LiteralPath $systemIcu -Destination (Join-Path $release '_internal\icuuc.dll') -Force
if ($helperRelease) {
    $helperLicenseRoot = Join-Path (Split-Path $helperRelease -Parent) 'licenses'
    if (Test-Path -LiteralPath $helperLicenseRoot -PathType Container) {
        Copy-Item -LiteralPath $helperLicenseRoot -Destination (Join-Path $release 'licenses') -Recurse -Force
    }
}
Write-Host "完成：$exe"
