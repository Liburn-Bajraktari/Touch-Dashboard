# build-windows.ps1
# Touch Dashboard — Windows build script
# Usage:
#   .\scripts\build-windows.ps1                 — full build (PyInstaller + ISCC)
#   .\scripts\build-windows.ps1 -SkipInstaller  — PyInstaller only (no ISCC)

param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# ── Locate Python ─────────────────────────────────────────────────────────────
$PythonExe = $null
foreach ($candidate in @("py", "python", "python3")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        $PythonExe = $candidate
        break
    }
}
if (-not $PythonExe) {
    Write-Error "Python not found. Install Python 3.x and ensure it is on PATH."
    exit 1
}

Write-Host "Using Python: $PythonExe ($((& $PythonExe --version 2>&1) -join ' '))"

# ── Read version from package.json ────────────────────────────────────────────
$Version = "unknown"
$PkgJson = Join-Path $Root "package.json"
if (Test-Path $PkgJson) {
    try {
        $pkg = Get-Content $PkgJson -Raw | ConvertFrom-Json
        $Version = $pkg.version
    } catch {}
}
Write-Host "Building Touch Dashboard v$Version"

# ── Install / upgrade dependencies ────────────────────────────────────────────
Write-Host "`n=== Installing Python dependencies ==="
& $PythonExe -m pip install --upgrade pip --quiet
& $PythonExe -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed."
    exit 1
}

# ── PyInstaller ───────────────────────────────────────────────────────────────
Write-Host -ForegroundColor Cyan "=== Running PyInstaller ==="
& $PythonExe -m PyInstaller --clean -y .\touch-dashboard.spec
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed."
    exit 1
}

# PyInstaller COLLECT mode outputs to dist\TouchDashboard\TouchDashboard.exe
$ExePath = Join-Path $Root "dist\TouchDashboard\TouchDashboard.exe"
if (-not (Test-Path $ExePath)) {
    Write-Error "PyInstaller succeeded but output exe not found at: $ExePath"
    exit 1
}

$ExeSize = (Get-Item $ExePath).Length
Write-Host "PyInstaller build complete: dist\TouchDashboard\TouchDashboard.exe  ($([Math]::Round($ExeSize / 1MB, 1)) MB)"

if ($SkipInstaller) {
    Write-Host "`nDone (installer skipped)."
    exit 0
}

# ── Locate Inno Setup ISCC ────────────────────────────────────────────────────
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
    Write-Host "`nInno Setup not found. Attempting install via winget..."
    winget install -e --id JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements
    $iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
}

if (-not (Test-Path $iscc)) {
    Write-Warning "Inno Setup not found. Skipping installer build."
    Write-Host "PyInstaller build complete: dist\TouchDashboard.exe"
    exit 0
}

# ── Patch version in installer.iss at build time ──────────────────────────────
# We replace the #define MyAppVersion line so package.json is the single source of truth
$IssPath = Join-Path $Root "installer.iss"
$IssContent = Get-Content $IssPath -Raw
$PatchedIss = $IssContent -replace '(#define MyAppVersion\s+")[^"]*(")', "`${1}$Version`${2}"

# Write to a temp copy so we don't dirty the working tree
$TmpIss = Join-Path $Root "installer_tmp.iss"
[System.IO.File]::WriteAllText($TmpIss, $PatchedIss, [System.Text.Encoding]::UTF8)

# ── Build installer ───────────────────────────────────────────────────────────
Write-Host "`n=== Compiling Inno Setup installer ==="
& $iscc $TmpIss
$IssExitCode = $LASTEXITCODE
Remove-Item $TmpIss -ErrorAction SilentlyContinue

if ($IssExitCode -ne 0) {
    Write-Error "Inno Setup compilation failed (exit $IssExitCode)."
    exit 1
}

$InstallerPath = Join-Path $Root "installers\TouchDashboardSetup.exe"
if (Test-Path $InstallerPath) {
    $InstallerSize = (Get-Item $InstallerPath).Length
    Write-Host "Installer build complete: installers\TouchDashboardSetup.exe  ($([Math]::Round($InstallerSize / 1MB, 1)) MB)"
} else {
    Write-Host "Installer build complete (output path not verified)."
}

Write-Host "`n=== Build Summary ==="
Write-Host "  Version  : v$Version"
Write-Host "  Exe      : dist\TouchDashboard.exe  ($([Math]::Round($ExeSize / 1MB, 1)) MB)"
if (Test-Path $InstallerPath) {
    Write-Host "  Installer: installers\TouchDashboardSetup.exe  ($([Math]::Round($InstallerSize / 1MB, 1)) MB)"
}
Write-Host "`nInstaller ready for testing: installers\TouchDashboardSetup.exe"
