$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $projectRoot

foreach ($directory in @('build', 'dist')) {
    $target = Join-Path $projectRoot $directory
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$uiResources = Join-Path $projectRoot 'src\ui\resources'
$assets = Join-Path $projectRoot 'assets'
$vendorRuntime = Join-Path $projectRoot 'vendor\ffmpeg'
$ffmpeg = Join-Path $projectRoot 'vendor\ffmpeg\ffmpeg.exe'
$license = Join-Path $projectRoot 'vendor\ffmpeg\LICENSE.txt'
$systemIcu = Join-Path $env:SystemRoot 'System32\icuuc.dll'
foreach ($required in @($python, $ffmpeg, $license, $systemIcu)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "缺少发布资源：$required"
    }
}
$runtimeDlls = @(Get-ChildItem -LiteralPath $vendorRuntime -Filter '*.dll' -File)
if ($runtimeDlls.Count -eq 0) {
    throw "缺少 FFmpeg shared runtime DLL：$vendorRuntime"
}

$pyInstallerArgs = @(
    '--noconfirm',
    '--clean',
    '--windowed',
    '--name', 'SocialMediaCollector',
    '--paths', '.',
    '--workpath', 'build',
    '--distpath', 'dist',
    '--specpath', 'build',
    '--add-data', "$uiResources;src\ui\resources",
    '--add-data', "$assets;assets",
    '--add-data', "$license;vendor\ffmpeg",
    '--add-binary', "$ffmpeg;vendor\ffmpeg",
    '--icon', (Join-Path $assets 'app_icon.ico'),
    'app.py'
)
foreach ($dll in $runtimeDlls) {
    $pyInstallerArgs += @('--add-binary', "$($dll.FullName);vendor\ffmpeg")
}

& $python -m PyInstaller @pyInstallerArgs

$release = Join-Path $projectRoot 'dist\SocialMediaCollector'
$exe = Join-Path $release 'SocialMediaCollector.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "构建完成但未找到 EXE：$exe"
}
# Qt6Core needs the Windows ICU build with its unsuffixed exports. PyInstaller
# can otherwise pick an incompatible ICU from another runtime on PATH.
Copy-Item -LiteralPath $systemIcu -Destination (Join-Path $release '_internal\icuuc.dll') -Force
Write-Host "完成：$exe"
