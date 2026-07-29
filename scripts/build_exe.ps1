$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --name "Pinterest Local Downloader" --paths . --add-data "src\ui\resources;src\ui\resources" app.py
Write-Host "完成：dist\Pinterest Local Downloader\Pinterest Local Downloader.exe"
