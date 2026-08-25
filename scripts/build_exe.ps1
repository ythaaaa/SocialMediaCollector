$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --name "Social Media Asset Collector" --paths . --add-data "src\ui\resources;src\ui\resources" --add-data "assets;assets" --icon "assets\app_icon.ico" app.py
Write-Host "完成：dist\Social Media Asset Collector\Social Media Asset Collector.exe"
