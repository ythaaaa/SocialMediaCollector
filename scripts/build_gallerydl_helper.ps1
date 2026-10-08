param(
    [Parameter(Mandatory = $true)]
    [string]$GalleryDlSource,
    [string]$BuildRoot = '',
    [string]$DistRoot = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $projectRoot

$expectedCommit = '8b9a56d6b1a53bc13a80894625fd58b24de3063a'
$expectedVersion = '1.32.14'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$entryPoint = Join-Path $projectRoot 'src\adapters\gallerydl_helper_entry.py'
$requirements = Join-Path $PSScriptRoot 'gallerydl_helper_runtime_requirements.txt'
$assets = Join-Path $projectRoot 'assets'

if (-not $BuildRoot) { $BuildRoot = Join-Path $projectRoot 'build-gallerydl-helper' }
if (-not $DistRoot) { $DistRoot = Join-Path $projectRoot 'dist-gallerydl-helper' }
$BuildRoot = [IO.Path]::GetFullPath($BuildRoot)
$DistRoot = [IO.Path]::GetFullPath($DistRoot)
$sourcePath = [IO.Path]::GetFullPath($GalleryDlSource)

foreach ($required in @($python, $entryPoint, $requirements, $assets)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "缺少 helper 构建输入：$required"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $sourcePath '.git') -PathType Container)) {
    throw "gallery-dl source 必须是 Git checkout：$sourcePath"
}
$actualCommit = (git -C $sourcePath rev-parse HEAD).Trim()
if ($actualCommit -ne $expectedCommit) {
    throw "gallery-dl commit 不匹配：期望 $expectedCommit，实际 $actualCommit"
}
foreach ($target in @($BuildRoot, $DistRoot)) {
    if (Test-Path -LiteralPath $target) {
        throw "为保证 clean helper build，输出目录必须不存在：$target"
    }
}

$snapshotZip = Join-Path $BuildRoot 'gallery-dl-source.zip'
$snapshotRoot = Join-Path $BuildRoot 'gallery-dl-source'
$galleryDlPackageRoot = Join-Path $snapshotRoot 'gallery_dl'
$dependencyRoot = Join-Path $BuildRoot 'runtime-site-packages'
$specRoot = Join-Path $BuildRoot 'spec'
$helperName = 'instagram-gallerydl-helper'

New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
git -C $sourcePath archive --format=zip $expectedCommit -o $snapshotZip
Expand-Archive -LiteralPath $snapshotZip -DestinationPath $snapshotRoot -Force

# gallery-dl's extractor registry imports module names dynamically from its
# own list. PyInstaller's automatic analysis cannot see those __import__
# calls, so derive explicit hidden imports from the fixed source snapshot.
$galleryDlModules = Get-ChildItem -LiteralPath $galleryDlPackageRoot -Filter '*.py' -File -Recurse |
    ForEach-Object {
        $relative = $_.FullName.Substring($galleryDlPackageRoot.Length + 1)
        $module = 'gallery_dl.' + (($relative -replace '\\', '.') -replace '\.py$', '')
        $module -replace '\.__init__$', ''
    } |
    Sort-Object -Unique

# Build dependencies are installed into a disposable target directory, never
# into the project venv or the verified gallery-dl development workspace.
& $python -m pip install --disable-pip-version-check --no-deps --target $dependencyRoot -r $requirements
if ($LASTEXITCODE -ne 0) { throw 'helper runtime dependency install failed' }
$certifiPem = Join-Path $dependencyRoot 'certifi\cacert.pem'
if (-not (Test-Path -LiteralPath $certifiPem -PathType Leaf)) {
    throw "certifi CA bundle is missing from helper runtime dependencies: $certifiPem"
}

$pyInstallerArgs = @(
    '--noconfirm',
    '--clean',
    '--console',
    '--name', $helperName,
    '--paths', $projectRoot,
    '--paths', $snapshotRoot,
    '--paths', $dependencyRoot,
    '--workpath', (Join-Path $BuildRoot 'pyinstaller-work'),
    '--distpath', $DistRoot,
    '--specpath', $specRoot,
    '--add-data', "$certifiPem;certifi",
    '--hidden-import', 'certifi',
    '--hidden-import', 'requests',
    '--hidden-import', 'urllib3',
    '--hidden-import', 'charset_normalizer',
    '--hidden-import', 'idna',
    '--icon', (Join-Path $assets 'app_icon.ico'),
    $entryPoint
)
foreach ($module in $galleryDlModules) {
    $pyInstallerArgs += @('--hidden-import', $module)
}
& $python -m PyInstaller @pyInstallerArgs
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller helper build failed' }

$release = Join-Path $DistRoot $helperName
$exe = Join-Path $release "$helperName.exe"
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "构建完成但未找到 helper EXE：$exe"
}

$licenseRoot = Join-Path $DistRoot 'licenses\gallery-dl'
New-Item -ItemType Directory -Path $licenseRoot -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $snapshotRoot 'LICENSE') -Destination (Join-Path $licenseRoot 'LICENSE') -Force
@("gallery-dl $expectedVersion", "commit $expectedCommit") |
    Set-Content -LiteralPath (Join-Path $licenseRoot 'VERSION') -Encoding utf8
@(
    'gallery-dl',
    "version $expectedVersion",
    "commit $expectedCommit",
    'upstream https://github.com/mikf/gallery-dl',
    'upstream modified by SocialMediaCollector: no'
) | Set-Content -LiteralPath (Join-Path $licenseRoot 'SOURCE.txt') -Encoding utf8

Write-Host "完成：$exe"
