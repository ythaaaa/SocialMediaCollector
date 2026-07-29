$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --name "Pinterest Local Downloader" --paths . app.py
Write-Host "完成：dist\Pinterest Local Downloader\Pinterest Local Downloader.exe"
