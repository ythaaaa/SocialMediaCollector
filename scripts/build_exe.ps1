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
$ffmpeg = Join-Path $projectRoot 'vendor\ffmpeg\ffmpeg.exe'
$license = Join-Path $projectRoot 'vendor\ffmpeg\LICENSE.txt'
foreach ($required in @($python, $ffmpeg, $license)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "缺少发布资源：$required"
    }
}

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name 'SocialMediaCollector' `
    --paths . `
    --workpath build `
    --distpath dist `
    --specpath build `
    --add-data "$uiResources;src\ui\resources" `
    --add-data "$assets;assets" `
    --add-data "$license;vendor\ffmpeg" `
    --add-binary "$ffmpeg;vendor\ffmpeg" `
    --icon (Join-Path $assets 'app_icon.ico') `
    app.py

$release = Join-Path $projectRoot 'dist\SocialMediaCollector'
$exe = Join-Path $release 'SocialMediaCollector.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "构建完成但未找到 EXE：$exe"
}
Write-Host "完成：$exe"
