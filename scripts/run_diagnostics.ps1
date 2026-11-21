$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$exe = Join-Path $projectRoot 'dist-diagnostics-output\SocialMediaCollector\SocialMediaCollector.exe'
$exe = [IO.Path]::GetFullPath($exe)
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "Diagnostics executable not found: $exe" }
$env:SMC_DIAGNOSTICS_HOME = Join-Path $projectRoot 'diagnostics-output'
$env:SMC_DIAGNOSTICS_HOME = [IO.Path]::GetFullPath($env:SMC_DIAGNOSTICS_HOME)
New-Item -ItemType Directory -Path $env:SMC_DIAGNOSTICS_HOME -Force | Out-Null
$process = Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe -Parent) -PassThru
Write-Host "Started diagnostic process $($process.Id)"
Write-Host "Diagnostics directory: $env:SMC_DIAGNOSTICS_HOME"
