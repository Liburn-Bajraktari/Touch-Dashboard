param (
    [switch]$Unattended
)

$ErrorActionPreference = "Stop"
$RepoOwner = "liburnb"
$RepoName = "Touch-Dashboard"
$InstallDir = "$env:LOCALAPPDATA\$RepoName"
$VbsLauncher = "$InstallDir\TouchDashboard.vbs"
$ZipUrl = "https://codeberg.org/$RepoOwner/$RepoName/archive/main.zip"
$ApiUrl = "https://codeberg.org/api/v1/repos/$RepoOwner/$RepoName"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$StartMenuPath = [Environment]::GetFolderPath("Programs")

# Helper Functions
function Write-Color {
    param([string]$text, [string]$color="White")
    Write-Host $text -ForegroundColor $color
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
    Find-Python

    Write-Color "[*] Downloading latest source code from Codeberg..." "Cyan"
    $tempZip = "$env:TEMP\TouchDashboard_main.zip"
    Invoke-WebRequest -Uri $ZipUrl -OutFile $tempZip
    
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

    # Firewall Rule
    Write-Color "[*] Configuring Windows Firewall for port 8888..." "Cyan"
    $ruleName = "Touch Dashboard Backend"
    $ruleExists = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if (-not $ruleExists) {
        try {
            New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -LocalPort 8888 -Protocol TCP -Action Allow -Profile Any -ErrorAction Stop | Out-Null
        } catch {
            Write-Color "  [!] Could not create firewall rule (requires admin). You may get a prompt when running the app." "Yellow"
        }
    }

    # Cleanup
    Remove-Item $tempZip -Force
    Remove-Item -Recurse -Force $extractDir

    Write-Color "`n[+] Installation Complete! You can launch the app from your desktop shortcut." "Green"
    Read-Host "Press Enter to return to menu..."
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
        Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -match "server.py" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

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
    Write-Color "[*] Fetching latest release from Codeberg API..." "Cyan"
    try {
        $releases = Invoke-RestMethod -Uri "$ApiUrl/releases"
        if ($releases.Count -eq 0) {
            Write-Color "  [-] No releases found on Codeberg." "Red"
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
        Write-Color "  [-] Failed to fetch release from Codeberg: $_" "Red"
    }
    Read-Host "Press Enter to return to menu..."
}

# ── Main Menu Loop ────────────────────────────────────────────────────────
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
