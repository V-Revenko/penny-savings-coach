# Build the Savings Coach installer:  installer\dist\SavingsCoach\  ->  installer\SavingsCoach.msi
# Needs: the project's .venv with PyInstaller installed, and the WiX toolset (dotnet tool "wix").
# Run from the project root:  powershell -File installer\build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
$wix = Join-Path $env:USERPROFILE ".dotnet-sdk\tools\wix.exe"
if (-not (Test-Path $wix)) { $wix = "wix" }
$env:DOTNET_ROOT = Join-Path $env:USERPROFILE ".dotnet-sdk"
$env:PATH = "$env:DOTNET_ROOT;$env:PATH"

# 1. Bundle Python + the app + data + static files. .env is deliberately NOT included.
& $py -m PyInstaller --noconfirm --clean --onedir --console --name SavingsCoach `
    --distpath installer\dist --workpath installer\build --specpath installer `
    --add-data "$root\data;data" --add-data "$root\static;static" `
    --collect-submodules uvicorn --collect-submodules anthropic --hidden-import openpyxl `
    installer\launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# 2. Wrap the folder in a per-user MSI (no admin rights needed, installs under %LOCALAPPDATA%\Programs).
& $wix build installer\Package.wxs -arch x64 -o installer\SavingsCoach.msi -d SourceDir="$root\installer\dist\SavingsCoach"
if ($LASTEXITCODE -ne 0) { throw "WiX build failed" }
Get-Item installer\SavingsCoach.msi | Select-Object Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } }
