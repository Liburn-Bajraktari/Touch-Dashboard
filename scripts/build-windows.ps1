param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m PyInstaller --clean .\touch-dashboard.spec

if ($SkipInstaller) {
    Write-Host "PyInstaller build complete: dist\TouchDashboard.exe"
    exit 0
}

$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    Write-Host "PyInstaller build complete: dist\TouchDashboard.exe"
    Write-Host "Inno Setup compiler ISCC.exe was not found on PATH. Install Inno Setup or rerun with -SkipInstaller."
    exit 0
}

& $iscc.Source .\installer.iss
Write-Host "Installer build complete: dist\installer\TouchDashboardSetup.exe"
