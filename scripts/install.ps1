param (
    [switch]$Unattended
)

$ErrorActionPreference = "Stop"
$RepoOwner = "Liburn-Bajraktari"
$RepoName = "Touch-Dashboard"
if ($env:TARGET_DIR) {
    $InstallDir = $env:TARGET_DIR
} else {
    $InstallDir = "$env:LOCALAPPDATA\$RepoName"
}
$VbsLauncher = "$InstallDir\TouchDashboard.vbs"
$ApiUrl = "https://api.github.com/repos/$RepoOwner/$RepoName"
$ReleasesUrl = "https://github.com/$RepoOwner/$RepoName/releases/download"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$StartMenuPath = [Environment]::GetFolderPath("Programs")

# Helper Functions
function Write-Color {
    param([string]$text, [string]$color="White")
    Write-Host $text -ForegroundColor $color
}

# The release to install: $env:TD_RELEASE_TAG (set by the app's self-update),
# otherwise the newest stable vX.Y.Z (not a draft or pre-release).
function Get-ReleaseTag {
    $tag = $env:TD_RELEASE_TAG
    if (-not $tag) {
        # Assigned first: piping Invoke-RestMethod straight on would not unroll the array in PowerShell 5.1.
        $releases = Invoke-RestMethod -Uri "$ApiUrl/releases" -Headers @{ Accept = "application/vnd.github+json" }
        $tag = ($releases | Where-Object { $_.tag_name -match '^v\d+\.\d+\.\d+$' -and -not $_.draft -and -not $_.prerelease } |
            Select-Object -First 1).tag_name
    }
    if ($tag -notmatch '^v\d+\.\d+\.\d+$') { throw "No Touch Dashboard release to install was found on GitHub." }
    return $tag
}

# Download release $tag's source archive and check it against the SHA256SUMS
# published with it. Returns the archive's path; throws on any mismatch.
function Get-VerifiedReleaseArchive {
    param([string]$tag)
    $archive = "touch-dashboard-$tag.zip"
    $zipPath = Join-Path $env:TEMP $archive
    $sumsPath = Join-Path $env:TEMP "TouchDashboard_SHA256SUMS"
    Invoke-WebRequest -Uri "$ReleasesUrl/$tag/SHA256SUMS" -OutFile $sumsPath -UseBasicParsing
    Invoke-WebRequest -Uri "$ReleasesUrl/$tag/$archive" -OutFile $zipPath -UseBasicParsing
    $line = Select-String -Path $sumsPath -Pattern (" \*?" + [regex]::Escape($archive) + '$') | Select-Object -First 1
    $expected = if ($line) { ($line.Line -split '\s+')[0] } else { "" }
    $actual = (Get-FileHash $zipPath -Algorithm SHA256).Hash
    Remove-Item $sumsPath -Force -ErrorAction SilentlyContinue
    if (-not $expected -or $actual -ne $expected) {
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
        throw "The download does not match the checksum published with $tag. Nothing was installed."
    }
    return $zipPath
}

# Ask a running app to quit. /api/exit takes the token the app writes to its
# data folder at startup; callers still stop whatever is left.
function Request-AppExit {
    $tokenFile = Join-Path $env:APPDATA "touch-dashboard\local_control.token"
    if (-not (Test-Path $tokenFile)) { return }
    $token = (Get-Content -Raw $tokenFile).Trim()
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:8888/api/exit" -Method Post -ContentType "application/json" `
            -Body "{}" -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 3 -ErrorAction SilentlyContinue | Out-Null
    } catch {}
}

function Find-Python {
    Write-Color "[*] Checking for Python..." "Cyan"
    $pythonFound = $false
    if (Get-Command "python" -ErrorAction SilentlyContinue) {
        $version = (python --version 2>&1)
        if ($version -match "Python 3") {
            Write-Color "  [+] Found Python: $version" "Green"
            $pythonFound = $true
        }
    }
    
    if (-not $pythonFound) {
        Write-Color "  [!] Python 3 not found or not in PATH." "Yellow"
        $install = Read-Host "  Would you like to install Python 3 using Winget? (Y/N)"
        if ($install -match "^[yY]") {
            Write-Color "  [*] Installing Python via Winget..." "Cyan"
            winget install Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
            Write-Color "  [+] Python installed! Please restart your terminal and run this installer again." "Green"
            exit
        } else {
            Write-Color "  [-] Python is required to proceed. Exiting." "Red"
            exit
        }
    }
}

function Install-Update {
    Write-Color "`n=== Installing / Updating Touch Dashboard ===" "Cyan"
    
    if (Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -match "server.py.*Touch-Dashboard"}) {
        Write-Color "  [!] Touch Dashboard is currently running." "Yellow"
        
        if ($env:AUTO_UPDATE -eq "1") {
            $ans = "Y"
        } else {
            $ans = Read-Host "  Would you like to close it to continue updating? (Y/N)"
        }
        
        if ($ans -match "^[Yy]$") {
            Write-Color "  [*] Stopping processes..." "Cyan"
            Request-AppExit
            Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -match "server.py.*Touch-Dashboard"} | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 1
        } else {
            Write-Color "  [-] Cannot update while the application is running." "Red"
            Read-Host "Press Enter to return to menu..."
            return
        }
    }

    Find-Python

    try {
        $releaseTag = Get-ReleaseTag
        Write-Color "[*] Downloading Touch Dashboard $releaseTag from GitHub and checking it against SHA256SUMS..." "Cyan"
        $tempZip = Get-VerifiedReleaseArchive $releaseTag
    } catch {
        Write-Color "  [-] $($_.Exception.Message)" "Red"
        if ($env:AUTO_UPDATE -eq "1") { exit 1 }
        Read-Host "Press Enter to return to menu..."
        return
    }
    
    Write-Color "[*] Extracting files..." "Cyan"
    if (Test-Path $InstallDir) {
        # Backup user data before extracting
        if (Test-Path "$InstallDir\config.json") { Copy-Item "$InstallDir\config.json" "$env:TEMP\config_backup.json" -Force }
        if (Test-Path "$InstallDir\.cache") { Copy-Item -Recurse "$InstallDir\.cache" "$env:TEMP\.cache_backup" -Force }
    } else {
        New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    }

    # Extract to temp
    $extractDir = "$env:TEMP\TouchDashboard_Extract"
    if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }
    Expand-Archive -Path $tempZip -DestinationPath $extractDir -Force
    
    # Move files from the 'touch-dashboard' subfolder to InstallDir
    $subDir = Get-ChildItem -Path $extractDir -Directory | Select-Object -First 1
    Copy-Item -Path "$($subDir.FullName)\*" -Destination $InstallDir -Recurse -Force
    
    # Restore user data
    if (Test-Path "$env:TEMP\config_backup.json") { Copy-Item "$env:TEMP\config_backup.json" "$InstallDir\config.json" -Force }
    if (Test-Path "$env:TEMP\.cache_backup") { Copy-Item -Recurse "$env:TEMP\.cache_backup" "$InstallDir\.cache" -Force }

    # Setup Python venv
    Write-Color "[*] Setting up Python virtual environment..." "Cyan"
    Set-Location $InstallDir
    
    $venvExists = Test-Path "$InstallDir\.venv"
    if ($venvExists) {
        $oldError = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $out = & "$InstallDir\.venv\Scripts\python.exe" --version 2>&1
        $ErrorActionPreference = $oldError
        
        if ($LASTEXITCODE -ne 0 -or "$out" -match "Could not find") {
            Write-Color "  [!] Existing virtual environment is corrupted or outdated. Recreating..." "Yellow"
            Remove-Item -Recurse -Force "$InstallDir\.venv" -ErrorAction SilentlyContinue
            $venvExists = $false
        }
    }

    $oldError = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    if (-not $venvExists) {
        python -m venv .venv 2>$null
    }
    
    Write-Color "[*] Installing Python dependencies..." "Cyan"
    & "$InstallDir\.venv\Scripts\python.exe" -m pip install --upgrade pip -q 2>$null
    & "$InstallDir\.venv\Scripts\python.exe" -m pip install -r requirements.txt -q 2>$null
    $ErrorActionPreference = $oldError
    
    # Create Silent Launcher (VBS)
    Write-Color "[*] Creating invisible background launcher..." "Cyan"
    $vbsContent = @"
Set objShell = CreateObject("WScript.Shell")
objShell.Run """$InstallDir\.venv\Scripts\pythonw.exe"" ""$InstallDir\server.py""", 0, False
"@
    Set-Content -Path $VbsLauncher -Value $vbsContent

    # Create Shortcuts
    Write-Color "[*] Creating Desktop and Start Menu shortcuts..." "Cyan"
    $WshShell = New-Object -comObject WScript.Shell
    
    # Desktop
    $Shortcut = $WshShell.CreateShortcut("$DesktopPath\Touch Dashboard.lnk")
    $Shortcut.TargetPath = "wscript.exe"
    $Shortcut.Arguments = """$VbsLauncher"""
    $Shortcut.WorkingDirectory = $InstallDir
    $Shortcut.IconLocation = "$InstallDir\static\favicon.ico"
    $Shortcut.WindowStyle = 7 # Minimized
    $Shortcut.Save()
    
    # Start Menu
    $ShortcutSM = $WshShell.CreateShortcut("$StartMenuPath\Touch Dashboard.lnk")
    $ShortcutSM.TargetPath = "wscript.exe"
    $ShortcutSM.Arguments = """$VbsLauncher"""
    $ShortcutSM.WorkingDirectory = $InstallDir
    $ShortcutSM.IconLocation = "$InstallDir\static\favicon.ico"
    $ShortcutSM.WindowStyle = 7
    $ShortcutSM.Save()

    # Firewall Rule: port 8888 on private networks only, and only from the local subnet.
    # On a network Windows classes as Public (cafés, hotels) the dashboard stays closed.
    Write-Color "[*] Configuring Windows Firewall for port 8888 (private networks only)..." "Cyan"
    $ruleName = "Touch Dashboard Backend"
    $ruleExists = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    try {
        if ($ruleExists) {
            # Older installers opened the port on every network profile, to any address.
            Set-NetFirewallRule -DisplayName $ruleName -Profile Private -RemoteAddress LocalSubnet -ErrorAction Stop
        } else {
            New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -LocalPort 8888 -Protocol TCP -Action Allow -Profile Private -RemoteAddress LocalSubnet -ErrorAction Stop | Out-Null
        }
        Write-Color "  [+] If the tablet cannot connect, set your home network to Private in Windows Settings > Network." "Green"
    } catch {
        if ($ruleExists) {
            Write-Color "  [!] Could not limit the existing firewall rule to private networks (requires admin). Run this installer as administrator once to fix it." "Yellow"
        } else {
            Write-Color "  [!] Could not create firewall rule (requires admin). You may get a prompt when running the app: allow it on private networks only." "Yellow"
        }
    }

    # Cleanup
    Remove-Item $tempZip -Force
    Remove-Item -Recurse -Force $extractDir

    Write-Color "`n[+] Installation Complete!" "Green"
    
    if ($env:AUTO_UPDATE -eq "1") {
        $launch = "Y"
    } else {
        $launch = Read-Host "[?] Would you like to launch Touch Dashboard now? (Y/N)"
    }

    if ($launch -match "^[Yy]$") {
        if ($env:AUTO_UPDATE -eq "1") {
            Start-Process "$InstallDir\.venv\Scripts\python.exe" -ArgumentList "server.py" -WorkingDirectory $InstallDir -WindowStyle Hidden
        } else {
            Start-Process "$DesktopPath\Touch Dashboard.lnk"
        }
    }
    
    if ($env:AUTO_UPDATE -ne "1") {
        Read-Host "`nPress Enter to return to menu..."
    }
}

function Repair-Installation {
    Write-Color "`n=== Repair Mode ===" "Cyan"
    $needsRepair = $false

    if (-not (Test-Path "$InstallDir\server.py")) {
        Write-Color "  [-] Core files missing." "Red"
        $needsRepair = $true
    } else {
        Write-Color "  [+] Core files found." "Green"
    }

    if (-not (Test-Path "$InstallDir\.venv\Scripts\python.exe")) {
        Write-Color "  [-] Python virtual environment missing." "Red"
        $needsRepair = $true
    } else {
        Write-Color "  [+] Virtual environment found." "Green"
    }

    if (-not (Test-Path "$VbsLauncher")) {
        Write-Color "  [-] VBS Launcher missing." "Red"
        $needsRepair = $true
    } else {
        Write-Color "  [+] VBS Launcher found." "Green"
    }

    if ($needsRepair) {
        Write-Color "`n[*] Repair needed. Running full update to restore missing files..." "Yellow"
        Install-Update
    } else {
        Write-Color "`n[+] No repairs needed. Your installation is intact!" "Green"
        Read-Host "Press Enter to return to menu..."
    }
}

function Uninstall-App {
    Write-Color "`n=== Uninstall Touch Dashboard ===" "Cyan"
    $confirm = Read-Host "Are you sure you want to completely uninstall? (Y/N)"
    if ($confirm -match "^[yY]") {
        
        # Kill running processes
        Write-Color "[*] Stopping backend processes..." "Cyan"
        Request-AppExit
        Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -match "server.py.*Touch-Dashboard"} | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }

        Start-Sleep -Seconds 2

        Write-Color "[*] Removing application files..." "Cyan"
        if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
        
        Write-Color "[*] Removing shortcuts..." "Cyan"
        if (Test-Path "$DesktopPath\Touch Dashboard.lnk") { Remove-Item "$DesktopPath\Touch Dashboard.lnk" -Force }
        if (Test-Path "$StartMenuPath\Touch Dashboard.lnk") { Remove-Item "$StartMenuPath\Touch Dashboard.lnk" -Force }

        Write-Color "[*] Removing firewall rules..." "Cyan"
        try { Remove-NetFirewallRule -DisplayName "Touch Dashboard Backend" -ErrorAction SilentlyContinue } catch {}

        Write-Color "[+] Uninstallation complete." "Green"
    }
    Read-Host "Press Enter to return to menu..."
}

function Get-APK {
    Write-Color "`n=== Download Android APK ===" "Cyan"
    Write-Color "[*] Fetching latest release from GitHub API..." "Cyan"
    try {
        $headers = @{ "Accept" = "application/vnd.github+json" }
        $releases = Invoke-RestMethod -Uri "$ApiUrl/releases" -Headers $headers -ErrorAction Stop
        if ($releases.Count -eq 0) {
            Write-Color "  [-] No releases found on GitHub." "Red"
            Read-Host "Press Enter to return to menu..."
            return
        }
        
        $latest = $releases[0]
        $apkAsset = $latest.assets | Where-Object { $_.name -like "*.apk" } | Select-Object -First 1
        
        if ($apkAsset) {
            Write-Color "  [+] Found APK: $($apkAsset.name) ($($latest.tag_name))" "Green"
            
            # Ask user for folder using FolderBrowserDialog
            Add-Type -AssemblyName System.Windows.Forms
            $folderBrowser = New-Object System.Windows.Forms.FolderBrowserDialog
            $folderBrowser.Description = "Select where to save the APK"
            $folderBrowser.ShowNewFolderButton = $true
            
            $result = $folderBrowser.ShowDialog()
            if ($result -eq 'OK') {
                $savePath = "$($folderBrowser.SelectedPath)\$($apkAsset.name)"
            } else {
                $savePath = "$([Environment]::GetFolderPath("UserProfile"))\Downloads\$($apkAsset.name)"
                Write-Color "  [!] Dialog cancelled. Defaulting to Downloads folder." "Yellow"
            }
            
            Write-Color "[*] Downloading to $savePath ..." "Cyan"
            Invoke-WebRequest -Uri $apkAsset.browser_download_url -OutFile $savePath
            Write-Color "[+] APK Downloaded successfully!" "Green"
            Invoke-Item $savePath | Out-Null
        } else {
            Write-Color "  [-] Latest release does not contain an APK asset." "Red"
        }
    } catch {
        if ($_.Exception.Response.StatusCode -eq "NotFound") {
            Write-Color "  [-] No releases have been published to this repository yet." "Yellow"
        } else {
            Write-Color "  [-] Failed to fetch release from GitHub: $($_.Exception.Message)" "Red"
        }
    }
    Read-Host "Press Enter to return to menu..."
}

# ── Main Menu Loop ────────────────────────────────────────────────────────

if ($env:AUTO_UPDATE -eq "1") {
    Install-Update
    exit 0
}

while ($true) {
    Clear-Host
    Write-Host ""
    Write-Host "    ████████╗ ██████╗ ██╗   ██╗ ██████╗██╗  ██╗" -ForegroundColor Cyan
    Write-Host "    ╚══██╔══╝██╔═══██╗██║   ██║██╔════╝██║  ██║" -ForegroundColor Cyan
    Write-Host "       ██║   ██║   ██║██║   ██║██║     ███████║" -ForegroundColor Cyan
    Write-Host "       ██║   ██║   ██║██║   ██║██║     ██╔══██║" -ForegroundColor Cyan
    Write-Host "       ██║   ╚██████╔╝╚██████╔╝╚██████╗██║  ██║" -ForegroundColor Cyan
    Write-Host "       ╚═╝    ╚═════╝  ╚═════╝  ╚═════╝╚═╝  ╚═╝" -ForegroundColor Cyan
    Write-Host "                         D A S H B O A R D     " -ForegroundColor White
    Write-Host ""
    Write-Host "   ╔══════════════════════════════════════════════════════════╗" -ForegroundColor DarkGray
    Write-Host "   ║                                                          ║" -ForegroundColor DarkGray
    Write-Host "   ║   " -ForegroundColor DarkGray -NoNewline; Write-Host " 1 " -ForegroundColor Cyan -NoNewline; Write-Host "   Install / Update Touch Dashboard                 " -ForegroundColor White -NoNewline; Write-Host "║" -ForegroundColor DarkGray
    Write-Host "   ║   " -ForegroundColor DarkGray -NoNewline; Write-Host " 2 " -ForegroundColor Yellow -NoNewline; Write-Host "   Repair Existing Installation                     " -ForegroundColor Gray -NoNewline; Write-Host "║" -ForegroundColor DarkGray
    Write-Host "   ║   " -ForegroundColor DarkGray -NoNewline; Write-Host " 3 " -ForegroundColor Red -NoNewline; Write-Host "   Completely Uninstall                             " -ForegroundColor Gray -NoNewline; Write-Host "║" -ForegroundColor DarkGray
    Write-Host "   ║   " -ForegroundColor DarkGray -NoNewline; Write-Host " 4 " -ForegroundColor Green -NoNewline; Write-Host "   Download Android APK Client                      " -ForegroundColor Gray -NoNewline; Write-Host "║" -ForegroundColor DarkGray
    Write-Host "   ║                                                          ║" -ForegroundColor DarkGray
    Write-Host "   ║   " -ForegroundColor DarkGray -NoNewline; Write-Host " 0 " -ForegroundColor DarkGray -NoNewline; Write-Host "   Exit Installer                                   " -ForegroundColor DarkGray -NoNewline; Write-Host "║" -ForegroundColor DarkGray
    Write-Host "   ║                                                          ║" -ForegroundColor DarkGray
    Write-Host "   ╚══════════════════════════════════════════════════════════╝" -ForegroundColor DarkGray
    Write-Host ""
    
    Write-Host "   > " -ForegroundColor Cyan -NoNewline
    $choice = Read-Host
    
    switch ($choice) {
        "1" { Install-Update }
        "2" { Repair-Installation }
        "3" { Uninstall-App }
        "4" { Get-APK }
        "0" { exit }
        default { Write-Color "   [!] Invalid option selected." "Red"; Start-Sleep -Seconds 1 }
    }
}
