param (
    [switch]$Unattended
)
# Touch Dashboard's Windows installer: install, update, repair, uninstall.
#
# ASCII only, on purpose: Windows PowerShell 5.1 reads a script without a
# byte order mark in the ANSI code page, where UTF-8 box characters turn into
# quotes and the script no longer parses.
#
# The app's own update runs it with AUTO_UPDATE=1, TD_RELEASE_TAG, TARGET_DIR
# and TD_WAIT_PID (the app's process). It downloads and checks the release,
# then hands over to a hidden copy of itself (TD_PHASE=finish) and says
# TD-HANDOFF; the app exits on that word, and the copy waits for it to be gone
# before it installs: Windows keeps a running app's packages locked, so pip
# cannot replace them under it. The copy logs to %APPDATA%\touch-dashboard\update.log
# and starts the new version.

$ErrorActionPreference = "Stop"
# GitHub takes TLS 1.2 or newer, which Windows PowerShell 5.1 may not offer unless asked.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$RepoOwner = "Liburn-Bajraktari"
$RepoName = "Touch-Dashboard"
if ($env:TARGET_DIR) {
    $InstallDir = $env:TARGET_DIR.TrimEnd('\')
} else {
    $InstallDir = "$env:LOCALAPPDATA\$RepoName"
}
$VbsLauncher = "$InstallDir\TouchDashboard.vbs"
$ApiUrl = "https://api.github.com/repos/$RepoOwner/$RepoName"
$ReleasesUrl = "https://github.com/$RepoOwner/$RepoName/releases/download"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$StartMenuPath = [Environment]::GetFolderPath("Programs")
$DataDir = Join-Path $env:APPDATA "touch-dashboard"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValue = "TouchDashboard"
$Auto = $env:AUTO_UPDATE -eq "1"
# What a release replaces in the install folder; .venv and anything else stays.
$AppParts = @("static", "templates", "wow_addons", "addon", "scripts", "tests", "android", "docs")

function Write-Color {
    param([string]$text, [string]$color = "White")
    Write-Host $text -ForegroundColor $color
}

# A step with how far along the update is: the app's progress bar reads the "NN.N%".
function Write-Step {
    param([string]$text, [double]$pct)
    Write-Color ("[*] {0} ({1:0.0}%)" -f $text, $pct) "Cyan"
}

function Stop-Installer {
    param([string]$text)
    Write-Color "  [-] $text" "Red"
    if ($Auto) { exit 1 }
    Read-Host "Press Enter to return to menu..."
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

# The SHA256 that release $tag's SHA256SUMS publishes for $name, or "".
function Get-PublishedHash {
    param([string]$tag, [string]$name)
    $sumsPath = Join-Path $env:TEMP "TouchDashboard_SHA256SUMS"
    Invoke-WebRequest -Uri "$ReleasesUrl/$tag/SHA256SUMS" -OutFile $sumsPath -UseBasicParsing
    $line = Select-String -Path $sumsPath -Pattern (" \*?" + [regex]::Escape($name) + '$') | Select-Object -First 1
    Remove-Item $sumsPath -Force -ErrorAction SilentlyContinue
    if ($line) { return ($line.Line -split '\s+')[0] }
    return ""
}

# Download release $tag's file $name to $path and check it against the
# release's SHA256SUMS. Throws on any mismatch, and leaves nothing behind.
function Get-VerifiedFile {
    param([string]$tag, [string]$name, [string]$path)
    $expected = Get-PublishedHash $tag $name
    Invoke-WebRequest -Uri "$ReleasesUrl/$tag/$name" -OutFile $path -UseBasicParsing
    $actual = (Get-FileHash $path -Algorithm SHA256).Hash
    if (-not $expected -or $actual -ne $expected) {
        Remove-Item $path -Force -ErrorAction SilentlyContinue
        throw "$name does not match the checksum published with $tag. Nothing was installed."
    }
}

# This install's running app: python(w).exe running server.py from the install
# folder. Windows PowerShell 5.1's process objects carry no command line; CIM does.
function Get-AppProcesses {
    $dir = $InstallDir.ToLower()
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains("server.py") -and
                       ($_.CommandLine.ToLower().Contains($dir) -or ("$($_.ExecutablePath)").ToLower().StartsWith($dir)) }
}

# Ask a running app to quit. /api/exit takes the token the app writes to its
# data folder at startup; whatever is left after a moment is stopped.
function Stop-App {
    $tokenFile = Join-Path $DataDir "local_control.token"
    if (Test-Path $tokenFile) {
        $token = (Get-Content -Raw $tokenFile).Trim()
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:8888/api/exit" -Method Post -ContentType "application/json" `
                -Body "{}" -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 3 -ErrorAction SilentlyContinue | Out-Null
        } catch {}
    }
    for ($i = 0; $i -lt 10 -and (Get-AppProcesses); $i++) { Start-Sleep -Milliseconds 500 }
    Get-AppProcesses | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# A Python pywebview's pythonnet and the winrt packages both run on: 3.11 to 3.13.
# The py launcher first (python.org's installer adds it, PATH or not), then python on PATH.
function Find-Python {
    # PowerShell 5.1 turns a program's stderr into errors, which "Stop" throws on: exit codes are checked instead.
    $ErrorActionPreference = "Continue"
    $wanted = @("3.13", "3.12", "3.11")
    if (Get-Command "py" -ErrorAction SilentlyContinue) {
        foreach ($v in $wanted) {
            $exe = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { return "$exe".Trim() }
        }
    }
    if (Get-Command "python" -ErrorAction SilentlyContinue) {
        $v = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $wanted -contains "$v".Trim()) {
            return (& python -c "import sys; print(sys.executable)").Trim()
        }
    }
    return $null
}

function Get-Python {
    Write-Color "[*] Checking for Python 3.11 to 3.13..." "Cyan"
    $exe = Find-Python
    if ($exe) {
        Write-Color "  [+] Found $exe" "Green"
        return $exe
    }
    if ($Auto) { throw "No Python 3.11 to 3.13 to make the app's environment with." }
    Write-Color "  [!] Python 3.11 to 3.13 was not found." "Yellow"
    $install = Read-Host "  Install Python 3.12 with winget? (Y/N)"
    if ($install -notmatch "^[yY]") { throw "Python is needed to run Touch Dashboard." }
    winget install Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    $exe = Find-Python
    if (-not $exe) { throw "Python was installed, but this window cannot see it yet: open a new one and run the installer again." }
    return $exe
}

# Replace the app's files with the release's in $from (the extracted archive).
# Its folders and top-level files go first, so nothing a release removed lingers:
# a module file left behind would still be loaded.
function Install-Files {
    param([string]$from)
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    foreach ($part in $AppParts) {
        if (Test-Path "$InstallDir\$part") { Remove-Item -Recurse -Force "$InstallDir\$part" }
    }
    Get-ChildItem -Path $InstallDir -File | Where-Object { $_.Extension -in ".py", ".txt", ".json", ".md", ".MD", ".sh", ".ps1" } |
        Remove-Item -Force
    Copy-Item -Path "$from\*" -Destination $InstallDir -Recurse -Force
}

# The app's Python environment, made if missing or broken, with requirements.txt
# installed. Throws with pip's own words when pip fails.
function Install-Environment {
    param([string]$log)
    $ErrorActionPreference = "Continue"     # pip warns on stderr; its exit code says whether it failed
    $venvPy = "$InstallDir\.venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        $null = & $venvPy -c "import sys" 2>&1
        if ($LASTEXITCODE -ne 0) {
            Remove-Item -Recurse -Force "$InstallDir\.venv" -ErrorAction SilentlyContinue
        }
    }
    if (-not (Test-Path $venvPy)) {
        $python = Get-Python
        $out = & $python -m venv "$InstallDir\.venv" 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Could not make the Python environment: $out" }
    }
    $out = & $venvPy -m pip install --upgrade pip --disable-pip-version-check -q 2>&1
    $out = & $venvPy -m pip install -r "$InstallDir\requirements.txt" --disable-pip-version-check 2>&1
    $code = $LASTEXITCODE
    if ($log) { $out | Out-File -FilePath $log -Append -Encoding utf8 }
    if ($code -ne 0) {
        throw ("pip could not install the requirements:`n" + (($out | Select-Object -Last 15) -join "`n"))
    }
}

# The silent launcher and the shortcuts. The launcher is UTF-16: VBScript reads
# that, so an install folder with accented letters works.
function Install-Shortcuts {
    $vbsContent = @"
Set objShell = CreateObject("WScript.Shell")
objShell.Run """$InstallDir\.venv\Scripts\pythonw.exe"" ""$InstallDir\server.py""", 0, False
"@
    Set-Content -Path $VbsLauncher -Value $vbsContent -Encoding Unicode
    $WshShell = New-Object -ComObject WScript.Shell
    foreach ($lnk in @("$DesktopPath\Touch Dashboard.lnk", "$StartMenuPath\Touch Dashboard.lnk")) {
        $Shortcut = $WshShell.CreateShortcut($lnk)
        $Shortcut.TargetPath = "wscript.exe"
        $Shortcut.Arguments = """$VbsLauncher"""
        $Shortcut.WorkingDirectory = $InstallDir
        $Shortcut.IconLocation = "$InstallDir\static\favicon.ico"
        $Shortcut.WindowStyle = 7
        $Shortcut.Save()
    }
}

# Start with Windows: the same Run value the app's own setting writes.
function Set-StartWithWindows {
    param([bool]$on)
    if ($on) {
        New-ItemProperty -Path $RunKey -Name $RunValue -Value "wscript.exe `"$VbsLauncher`"" -PropertyType String -Force | Out-Null
    } else {
        Remove-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue
    }
}

# Firewall rule: port 8888 on private networks only, and only from the local subnet.
# On a network Windows classes as Public (cafes, hotels) the dashboard stays closed.
function Set-FirewallRule {
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
}

function Start-App {
    Start-Process "$InstallDir\.venv\Scripts\pythonw.exe" -ArgumentList "`"$InstallDir\server.py`"" -WorkingDirectory $InstallDir
}

# Download release $tag, check it, and extract it to a folder of its own. Returns that folder.
function Get-Release {
    param([string]$tag)
    $zip = Join-Path $env:TEMP "touch-dashboard-$tag.zip"
    Get-VerifiedFile $tag "touch-dashboard-$tag.zip" $zip
    Write-Step "Downloaded and checked against SHA256SUMS" 40
    $extract = Join-Path $env:TEMP "TouchDashboard_Update_$tag"
    if (Test-Path $extract) { Remove-Item -Recurse -Force $extract }
    Expand-Archive -Path $zip -DestinationPath $extract -Force
    Remove-Item $zip -Force
    # The archive holds one folder, touch-dashboard-<tag>
    return (Get-ChildItem -Path $extract -Directory | Select-Object -First 1).FullName
}

function Install-Update {
    Write-Color "`n=== Installing / Updating Touch Dashboard ===" "Cyan"
    try {
        $tag = Get-ReleaseTag
        Write-Step "Downloading Touch Dashboard $tag from GitHub" 10
        $from = Get-Release $tag
        Write-Step "Extracted" 50
    } catch {
        return Stop-Installer $_.Exception.Message
    }

    if ($Auto) {
        # The app is still running: a hidden copy of this script installs once it is gone.
        $env:TD_PHASE = "finish"
        $env:TD_STAGED = $from
        Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
        Write-Step "Touch Dashboard closes now and opens again once $tag is installed" 60
        Write-Output "TD-HANDOFF"
        exit 0
    }

    if (Get-AppProcesses) {
        Write-Color "  [!] Touch Dashboard is running." "Yellow"
        $ans = Read-Host "  Close it to continue updating? (Y/N)"
        if ($ans -notmatch "^[Yy]$") { return Stop-Installer "Cannot update while the application is running." }
        Write-Color "[*] Stopping it..." "Cyan"
        Stop-App
    }
    try {
        Write-Color "[*] Copying files..." "Cyan"
        Install-Files $from
        Write-Color "[*] Setting up Python and its packages (this can take a few minutes)..." "Cyan"
        Install-Environment $null
        Install-Shortcuts
        Remove-Item -Recurse -Force (Split-Path $from -Parent) -ErrorAction SilentlyContinue
    } catch {
        return Stop-Installer $_.Exception.Message
    }
    Set-FirewallRule
    $startup = Read-Host "[?] Start Touch Dashboard when you sign in to Windows? (Y/N)"
    Set-StartWithWindows ($startup -match "^[Yy]$")
    Write-Color "`n[+] Installation Complete!" "Green"
    $launch = Read-Host "[?] Would you like to launch Touch Dashboard now? (Y/N)"
    if ($launch -match "^[Yy]$") { Start-App }
    Read-Host "`nPress Enter to return to menu..."
}

# The hidden second half of an update from the app: no console, a log file.
function Complete-Update {
    $staged = $env:TD_STAGED
    # The app started below inherits this environment: with TD_PHASE still set,
    # its next update would skip straight to this half, with a folder long gone.
    Remove-Item Env:TD_PHASE, Env:TD_STAGED -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    $log = Join-Path $DataDir "update.log"
    "`n=== $(Get-Date -Format s) updating $InstallDir from $staged ===" | Out-File -FilePath $log -Append -Encoding utf8
    try {
        if ($env:TD_WAIT_PID) {
            # The app exits once it has read TD-HANDOFF; one that hangs is stopped.
            Wait-Process -Id ([int]$env:TD_WAIT_PID) -Timeout 60 -ErrorAction SilentlyContinue
            Stop-Process -Id ([int]$env:TD_WAIT_PID) -Force -ErrorAction SilentlyContinue
        }
        Stop-App
        Install-Files $staged
        Install-Environment $log
        Install-Shortcuts
        "Installed." | Out-File -FilePath $log -Append -Encoding utf8
    } catch {
        "FAILED: $($_.Exception.Message)" | Out-File -FilePath $log -Append -Encoding utf8
    } finally {
        Remove-Item -Recurse -Force (Split-Path $staged -Parent) -ErrorAction SilentlyContinue
        # Whatever happened, the app comes back: the old version if the copy failed early.
        if (Test-Path "$InstallDir\.venv\Scripts\pythonw.exe") { Start-App }
        Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    }
    exit 0
}

function Repair-Installation {
    Write-Color "`n=== Repair Mode ===" "Cyan"
    $needsRepair = $false
    foreach ($check in @(@("$InstallDir\server.py", "Core files"), @("$InstallDir\.venv\Scripts\python.exe", "Python environment"),
                         @($VbsLauncher, "Launcher"))) {
        if (Test-Path $check[0]) {
            Write-Color "  [+] $($check[1]) found." "Green"
        } else {
            Write-Color "  [-] $($check[1]) missing." "Red"
            $needsRepair = $true
        }
    }
    if ($needsRepair) {
        Write-Color "`n[*] Repair needed. Running a full install to restore what is missing..." "Yellow"
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
        Write-Color "[*] Stopping Touch Dashboard..." "Cyan"
        Stop-App
        Write-Color "[*] Removing application files..." "Cyan"
        if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
        Write-Color "[*] Removing shortcuts and start with Windows..." "Cyan"
        foreach ($lnk in @("$DesktopPath\Touch Dashboard.lnk", "$StartMenuPath\Touch Dashboard.lnk")) {
            if (Test-Path $lnk) { Remove-Item $lnk -Force }
        }
        Set-StartWithWindows $false
        Write-Color "[*] Removing firewall rules..." "Cyan"
        try { Remove-NetFirewallRule -DisplayName "Touch Dashboard Backend" -ErrorAction SilentlyContinue } catch {}
        if (Test-Path $DataDir) {
            $data = Read-Host "[?] Also delete your settings, paired tablets and sign-ins ($DataDir)? (Y/N)"
            if ($data -match "^[yY]") { Remove-Item -Recurse -Force $DataDir }
        }
        Write-Color "[+] Uninstallation complete." "Green"
    }
    Read-Host "Press Enter to return to menu..."
}

function Get-APK {
    Write-Color "`n=== Download Android APK ===" "Cyan"
    Write-Color "[*] Fetching latest release from GitHub API..." "Cyan"
    try {
        $releases = Invoke-RestMethod -Uri "$ApiUrl/releases" -Headers @{ "Accept" = "application/vnd.github+json" } -ErrorAction Stop
        $latest = $releases | Where-Object { -not $_.draft -and -not $_.prerelease -and ($_.assets | Where-Object { $_.name -like "*.apk" }) } |
            Select-Object -First 1
        if (-not $latest) {
            Write-Color "  [-] No release with an APK was found." "Red"
            Read-Host "Press Enter to return to menu..."
            return
        }
        $apkAsset = $latest.assets | Where-Object { $_.name -like "*.apk" } | Select-Object -First 1
        Write-Color "  [+] Found APK: $($apkAsset.name) ($($latest.tag_name))" "Green"
        Add-Type -AssemblyName System.Windows.Forms
        $folderBrowser = New-Object System.Windows.Forms.FolderBrowserDialog
        $folderBrowser.Description = "Select where to save the APK"
        $folderBrowser.ShowNewFolderButton = $true
        if ($folderBrowser.ShowDialog() -eq 'OK') {
            $savePath = "$($folderBrowser.SelectedPath)\$($apkAsset.name)"
        } else {
            $savePath = "$([Environment]::GetFolderPath("UserProfile"))\Downloads\$($apkAsset.name)"
            Write-Color "  [!] Dialog cancelled. Defaulting to Downloads folder." "Yellow"
        }
        Write-Color "[*] Downloading to $savePath and checking it against SHA256SUMS..." "Cyan"
        Get-VerifiedFile $latest.tag_name $apkAsset.name $savePath
        Write-Color "[+] APK downloaded and checked." "Green"
        Invoke-Item (Split-Path $savePath -Parent) | Out-Null
    } catch {
        Write-Color "  [-] $($_.Exception.Message)" "Red"
    }
    Read-Host "Press Enter to return to menu..."
}

# -- Main -------------------------------------------------------------------

if ($env:TD_PHASE -eq "finish") {
    Complete-Update
}
if ($Auto) {
    Install-Update
    exit 0
}

while ($true) {
    Clear-Host
    Write-Host ""
    Write-Host "    _____                 _       ____            _     _                         _ " -ForegroundColor Cyan
    Write-Host "   |_   _|__  _   _  ___| |__   |  _ \  __ _ ___| |__ | |__   ___   __ _ _ __ __| |" -ForegroundColor Cyan
    Write-Host "     | |/ _ \| | | |/ __| '_ \  | | | |/ _' / __| '_ \| '_ \ / _ \ / _' | '__/ _' |" -ForegroundColor Cyan
    Write-Host "     | | (_) | |_| | (__| | | | | |_| | (_| \__ \ | | | |_) | (_) | (_| | | | (_| |" -ForegroundColor Cyan
    Write-Host "     |_|\___/ \__,_|\___|_| |_| |____/ \__,_|___/_| |_|_.__/ \___/ \__,_|_|  \__,_|" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "   +----------------------------------------------------------+" -ForegroundColor DarkGray
    Write-Host "   |   " -ForegroundColor DarkGray -NoNewline; Write-Host " 1 " -ForegroundColor Cyan -NoNewline; Write-Host "   Install / Update Touch Dashboard                 " -ForegroundColor White -NoNewline; Write-Host "|" -ForegroundColor DarkGray
    Write-Host "   |   " -ForegroundColor DarkGray -NoNewline; Write-Host " 2 " -ForegroundColor Yellow -NoNewline; Write-Host "   Repair Existing Installation                     " -ForegroundColor Gray -NoNewline; Write-Host "|" -ForegroundColor DarkGray
    Write-Host "   |   " -ForegroundColor DarkGray -NoNewline; Write-Host " 3 " -ForegroundColor Red -NoNewline; Write-Host "   Completely Uninstall                             " -ForegroundColor Gray -NoNewline; Write-Host "|" -ForegroundColor DarkGray
    Write-Host "   |   " -ForegroundColor DarkGray -NoNewline; Write-Host " 4 " -ForegroundColor Green -NoNewline; Write-Host "   Download Android APK Client                      " -ForegroundColor Gray -NoNewline; Write-Host "|" -ForegroundColor DarkGray
    Write-Host "   |                                                          |" -ForegroundColor DarkGray
    Write-Host "   |   " -ForegroundColor DarkGray -NoNewline; Write-Host " 0 " -ForegroundColor DarkGray -NoNewline; Write-Host "   Exit Installer                                   " -ForegroundColor DarkGray -NoNewline; Write-Host "|" -ForegroundColor DarkGray
    Write-Host "   +----------------------------------------------------------+" -ForegroundColor DarkGray
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
