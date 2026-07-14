param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

py -m pip install --upgrade pip
py -m pip install -r requirements.txt
py -m PyInstaller --clean .\touch-dashboard.spec

if ($SkipInstaller) {
    Write-Host "PyInstaller build complete: dist\TouchDashboard.exe"
    exit 0
}

# Look for Inno Setup compiler
$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    $paths = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    foreach ($p in $paths) {
        if (Test-Path $p) {
            $iscc = $p
            break
        }
    }
}

if (-not $iscc) {
    Write-Host "Inno Setup compiler not found. Installing via winget..."
    winget install -e --id JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements
    $iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
}

if (Test-Path $iscc) {
    Write-Host "Compiling GUI Installer Wizard..."
    & $iscc .\installer.iss
    Write-Host "Installer build complete: installers\TouchDashboardSetup.exe"
} else {
    Write-Host "Failed to find or install Inno Setup. PyInstaller build complete."
}
