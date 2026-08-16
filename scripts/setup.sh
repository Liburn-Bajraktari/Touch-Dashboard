#!/usr/bin/env bash

# Touch Dashboard Linux Installer
# Run via: bash -c "$(curl -fsSL https://codeberg.org/liburnb/Touch-Dashboard/raw/branch/main/scripts/setup.sh)" < /dev/tty

set -e

REPO_OWNER="liburnb"
REPO_NAME="Touch-Dashboard"
INSTALL_DIR="$HOME/.local/share/$REPO_NAME"
DESKTOP_ENTRY_DIR="$HOME/.local/share/applications"
ZIP_URL="https://codeberg.org/$REPO_OWNER/$REPO_NAME/archive/main.tar.gz"
API_URL="https://codeberg.org/api/v1/repos/$REPO_OWNER/$REPO_NAME"

# Colors
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color
GRAY='\033[1;30m'

write_color() {
    local text="$1"
    local color="$2"
    echo -e "${color}${text}${NC}"
}

get_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$ID"
    else
        echo "unknown"
    fi
}

install_deps() {
    local distro=$(get_distro)
    write_color "[*] Checking system dependencies for '$distro'..." "$CYAN"
    
    if [ "$distro" = "arch" ] || [ "$distro" = "manjaro" ] || [ "$distro" = "endeavouros" ] || grep -q "arch" /etc/os-release 2>/dev/null; then
        write_color "  [*] Installing via pacman (requires sudo)..." "$CYAN"
        sudo pacman -S --needed --noconfirm python python-pip playerctl pipewire wireplumber webkit2gtk-4.1
    elif [ "$distro" = "ubuntu" ] || [ "$distro" = "debian" ] || [ "$distro" = "pop" ] || [ "$distro" = "linuxmint" ]; then
        write_color "  [*] Installing via apt (requires sudo)..." "$CYAN"
        sudo apt-get update
        sudo apt-get install -y python3 python3-venv python3-pip playerctl pipewire wireplumber libwebkit2gtk-4.1-dev
    elif [ "$distro" = "fedora" ]; then
        write_color "  [*] Installing via dnf (requires sudo)..." "$CYAN"
        sudo dnf install -y python3 playerctl pipewire wireplumber webkit2gtk4.1
    else
        write_color "  [!] Unsupported distribution. Please ensure Python 3, playerctl, pipewire, and webkit2gtk-4.1 are installed manually." "$YELLOW"
        read -p "  Press Enter to continue once you have installed them..."
    fi
}

install_update() {
    echo -e "\n${CYAN}=== Installing / Updating Touch Dashboard ===${NC}"
    
    # Check if running
    if pgrep -f "server.py.*Touch-Dashboard" > /dev/null; then
        write_color "  [!] Touch Dashboard is currently running." "$YELLOW"
        read -p "  Would you like to close it to continue updating? (Y/N): " ans || true
        if [[ "$ans" =~ ^[Yy]$ ]]; then
            write_color "  [*] Stopping processes..." "$CYAN"
            curl -s -X POST http://127.0.0.1:8888/api/exit > /dev/null || true
            pkill -f "server.py.*Touch-Dashboard" || true
            sleep 1
        else
            write_color "  [-] Cannot update while the application is running." "$RED"
            read -p "Press Enter to return to menu..."
            return
        fi
    fi

    install_deps

    write_color "[*] Downloading latest source code from Codeberg..." "$CYAN"
    TEMP_DIR=$(mktemp -d)
    TEMP_TAR="$TEMP_DIR/TouchDashboard_main.tar.gz"
    curl -L "$ZIP_URL" -o "$TEMP_TAR"
    
    write_color "[*] Extracting files..." "$CYAN"
    mkdir -p "$INSTALL_DIR"
    
    # Backup user data
    if [ -f "$INSTALL_DIR/config.json" ]; then cp "$INSTALL_DIR/config.json" "$TEMP_DIR/config_backup.json"; fi
    if [ -d "$INSTALL_DIR/.cache" ]; then cp -r "$INSTALL_DIR/.cache" "$TEMP_DIR/.cache_backup"; fi

    # Extract
    tar -xzf "$TEMP_TAR" -C "$TEMP_DIR"
    EXTRACTED_DIR=$(find "$TEMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)
    
    if [ -z "$EXTRACTED_DIR" ]; then
        write_color "  [-] Failed to find extracted repository directory." "$RED"
        read -p "Press Enter to return to menu..." || true
        return
    fi
    
    cp -r "$EXTRACTED_DIR/"* "$INSTALL_DIR/"

    # Restore user data
    if [ -f "$TEMP_DIR/config_backup.json" ]; then cp "$TEMP_DIR/config_backup.json" "$INSTALL_DIR/config.json"; fi
    if [ -d "$TEMP_DIR/.cache_backup" ]; then cp -r "$TEMP_DIR/.cache_backup" "$INSTALL_DIR/.cache"; fi

    write_color "[*] Setting up Python virtual environment..." "$CYAN"
    cd "$INSTALL_DIR"
    
    if [ ! -d "$INSTALL_DIR/.venv" ] || [ ! -f "$INSTALL_DIR/.venv/bin/python" ]; then
        python3 -m venv .venv
    fi

    write_color "[*] Installing Python dependencies..." "$CYAN"
    "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip -q
    "$INSTALL_DIR/.venv/bin/python" -m pip install -r requirements.txt -q

    write_color "[*] Creating Application Launcher..." "$CYAN"
    mkdir -p "$DESKTOP_ENTRY_DIR"
    cat > "$DESKTOP_ENTRY_DIR/touch-dashboard.desktop" <<EOF
[Desktop Entry]
Name=Touch Dashboard
Comment=System dashboard and macro pad
Exec=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/server.py
Icon=$INSTALL_DIR/static/icon-512.png
Terminal=false
Type=Application
Categories=Utility;
EOF
    chmod +x "$DESKTOP_ENTRY_DIR/touch-dashboard.desktop"

    # Cleanup
    rm -rf "$TEMP_DIR"

    write_color "\n[+] Installation Complete!" "$GREEN"
    read -p "[?] Would you like to launch Touch Dashboard now? (Y/N): " launch || true
    if [[ "$launch" =~ ^[Yy]$ ]]; then
        gtk-launch touch-dashboard.desktop || (cd "$INSTALL_DIR" && "$INSTALL_DIR/.venv/bin/python" server.py &)
    fi
    
    read -p $'\nPress Enter to return to menu...' || true
}

repair() {
    echo -e "\n${CYAN}=== Repair Mode ===${NC}"
    local needs_repair=false

    if [ ! -f "$INSTALL_DIR/server.py" ]; then
        write_color "  [-] Core files missing." "$RED"
        needs_repair=true
    else
        write_color "  [+] Core files found." "$GREEN"
    fi

    if [ ! -f "$INSTALL_DIR/.venv/bin/python" ]; then
        write_color "  [-] Python virtual environment missing." "$RED"
        needs_repair=true
    else
        write_color "  [+] Virtual environment found." "$GREEN"
    fi

    if [ ! -f "$DESKTOP_ENTRY_DIR/touch-dashboard.desktop" ]; then
        write_color "  [-] Desktop launcher missing." "$RED"
        needs_repair=true
    else
        write_color "  [+] Desktop launcher found." "$GREEN"
    fi

    if [ "$needs_repair" = true ]; then
        write_color "\n[*] Repair needed. Running full update to restore missing files..." "$YELLOW"
        install_update
    else
        write_color "\n[+] No repairs needed. Your installation is intact!" "$GREEN"
        read -p "Press Enter to return to menu..." || true
    fi
}

uninstall() {
    echo -e "\n${CYAN}=== Uninstall Touch Dashboard ===${NC}"
    read -p "Are you sure you want to completely uninstall? (Y/N): " confirm || true
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        
        write_color "[*] Stopping backend processes..." "$CYAN"
        curl -s -X POST http://127.0.0.1:8888/api/exit > /dev/null || true
        pkill -f "server.py.*Touch-Dashboard" || true
        sleep 2

        write_color "[*] Removing application files..." "$CYAN"
        rm -rf "$INSTALL_DIR"
        
        write_color "[*] Removing shortcuts..." "$CYAN"
        rm -f "$DESKTOP_ENTRY_DIR/touch-dashboard.desktop"

        write_color "[+] Uninstallation complete." "$GREEN"
    fi
    read -p "Press Enter to return to menu..."
}

get_apk() {
    echo -e "\n${CYAN}=== Download Android APK ===${NC}"
    write_color "[*] Fetching latest release from Codeberg API..." "$CYAN"
    
    LATEST_JSON=$(curl -s "$API_URL/releases")
    if echo "$LATEST_JSON" | grep -q '"message": "Not Found"'; then
        write_color "  [-] No releases found on Codeberg." "$YELLOW"
        read -p "Press Enter to return to menu..."
        return
    fi
    
    # Extract browser_download_url using grep/sed since jq might not be installed
    DOWNLOAD_URL=$(echo "$LATEST_JSON" | grep -o '"browser_download_url": *"[^"]*apk"' | head -1 | cut -d'"' -f4)
    FILE_NAME=$(basename "$DOWNLOAD_URL" 2>/dev/null || echo "TouchDashboard.apk")
    
    if [ -n "$DOWNLOAD_URL" ]; then
        write_color "  [+] Found APK: $FILE_NAME" "$GREEN"
        
        SAVE_DIR="$HOME/Downloads"
        write_color "[*] Downloading to $SAVE_DIR/$FILE_NAME ..." "$CYAN"
        curl -L "$DOWNLOAD_URL" -o "$SAVE_DIR/$FILE_NAME"
        
        write_color "[+] APK Downloaded successfully!" "$GREEN"
    else
        write_color "  [-] Latest release does not contain an APK asset." "$RED"
    fi
    read -p "Press Enter to return to menu..."
}

while true; do
    clear || true
    echo -e ""
    echo -e "${CYAN}    ████████╗ ██████╗ ██╗   ██╗ ██████╗██╗  ██╗${NC}"
    echo -e "${CYAN}    ╚══██╔══╝██╔═══██╗██║   ██║██╔════╝██║  ██║${NC}"
    echo -e "${CYAN}       ██║   ██║   ██║██║   ██║██║     ███████║${NC}"
    echo -e "${CYAN}       ██║   ██║   ██║██║   ██║██║     ██╔══██║${NC}"
    echo -e "${CYAN}       ██║   ╚██████╔╝╚██████╔╝╚██████╗██║  ██║${NC}"
    echo -e "${CYAN}       ╚═╝    ╚═════╝  ╚═════╝  ╚═════╝╚═╝  ╚═╝${NC}"
    echo -e "${NC}                         D A S H B O A R D     ${NC}"
    echo -e ""
    echo -e "${GRAY}   ╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GRAY}   ║                                                          ║${NC}"
    echo -e "${GRAY}   ║    ${CYAN}1${NC}    Install / Update Touch Dashboard                 ${GRAY}║${NC}"
    echo -e "${GRAY}   ║    ${YELLOW}2${NC}    Repair Existing Installation                     ${GRAY}║${NC}"
    echo -e "${GRAY}   ║    ${RED}3${NC}    Completely Uninstall                             ${GRAY}║${NC}"
    echo -e "${GRAY}   ║    ${GREEN}4${NC}    Download Android APK Client                      ${GRAY}║${NC}"
    echo -e "${GRAY}   ║                                                          ║${NC}"
    echo -e "${GRAY}   ║    ${GRAY}0    Exit Installer                                   ║${NC}"
    echo -e "${GRAY}   ║                                                          ║${NC}"
    echo -e "${GRAY}   ╚══════════════════════════════════════════════════════════╝${NC}"
    echo -e ""
    echo -ne "   ${CYAN}> ${NC}"
    read choice || choice=""
    
    case $choice in
        1) install_update ;;
        2) repair ;;
        3) uninstall ;;
        4) get_apk ;;
        0) exit 0 ;;
        *) write_color "   [!] Invalid option selected." "$RED"; sleep 1 ;;
    esac
done
