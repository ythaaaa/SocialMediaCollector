$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
py -3 -m PyInstaller --noconfirm --clean --windowed --name "Pinterest Local Downloader" --paths . app.py
Write-Host "完成：dist\Pinterest Local Downloader\Pinterest Local Downloader.exe"
