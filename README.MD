# Touch Dashboard

## Heads Up

This project is a tablet-oriented macro pad and system dashboard, intended as an alternative to tools like Stream Deck or Touch Portal.

The code itself is mostly AI-generated, but the logic is human-reviewed. It currently exists mainly to fit my own workflow and hardware setup. I do plan to make it more modular and generally useful over time, and contributions are welcome from anyone who wants to improve it.

## How to Install

### Windows

Touch Dashboard uses a modern PowerShell web installer to completely bypass Windows Smart App Control and ensure a clean, silent background installation.

1. Open PowerShell and run the following command:
   ```powershell
   irm https://codeberg.org/liburnb/Touch-Dashboard/raw/branch/main/scripts/install.ps1 | iex
   ```
2. The installer will pop up a text-based menu. Press **1** to automatically download the app, setup Python, and create your shortcuts.
3. Once it's done, you can launch Touch Dashboard directly from your desktop or Start Menu. It will run completely invisibly in the background.

*(The installer script also features a Repair Mode, an Uninstaller, and an option to download the latest Android APK directly to your PC).*

### Android

1. Download the latest `app-release.apk` from the Releases page (or download it directly to your PC using option 4 in the Windows PowerShell installer).
2. Transfer the file to your Android tablet or phone and install it. (You may need to allow "Install from Unknown Sources" in your Android security settings).

## How to Set up Everything

### 1. The Desktop App (Backend)
Touch Dashboard operates on a Client/Server model. The backend server must be running on your main PC at all times in order to intercept hardware metrics and handle system controls.

Launch the `Touch Dashboard` app from your Start Menu. It will quietly run in the background. You can find its icon in the System Tray (bottom right corner of your taskbar). Right-click the tray icon and look at the top of the menu to find your **Local IP Address**. You will need this for your tablet.

### 2. Configuring Everything
Once the backend is running on your PC, open a web browser on that same PC and navigate to `http://127.0.0.1:8888`. Double-click anywhere outside a button to open the **Settings Menu**.

- **Spotify:** Create a Spotify Developer app. Enter your Client ID and Secret in the settings. Ensure the Redirect URI in the developer console is set exactly to `http://127.0.0.1:8888/callback`. You *must* perform the initial authentication from your host PC.
- **Discord:** Create a Discord Developer Application. Enter the Client ID and Secret. Set the Redirect URI to `http://127.0.0.1:8888/disc_callback`. Discord must be running locally for IPC to work.
- **Weather:** Enter an OpenWeatherMap API key and your city name.
- **Audio Routing:** The dashboard automatically pulls your active audio devices. You can assign them short display names here so they look clean on the dashboard.
- *Click "Save & Apply" after entering your settings to apply them instantly.*

### 3. Connecting and Using the Android App
1. Make sure your Android tablet/phone is connected to the same Wi-Fi network as your host PC.
2. Open the Touch Dashboard app on Android.
3. On first launch, it will ask for your desktop's IP address. (You found this earlier in the system tray menu).
4. Enter the IP (e.g., `192.168.1.50`) and connect. The app will cache this IP so you don't have to enter it again!

**Touch Controls:**
- **Two-finger double tap:** Opens the Settings menu.
- **Sliders:** Drag up/down to adjust volume. Tap the icon below them to instantly mute/unmute.
- **Output Cycle:** Tap the active device button in the middle of the audio panel to cycle through your available speakers/headphones.

## Extra Notes

- **Native Windows Integrations:** The Windows backend deeply integrates with native COM, WASAPI (`pycaw`), and WinRT (`Windows.Media.Control`). It interacts asynchronously with the system audio and media layers to ensure 0% CPU overhead while idling. Threading is carefully managed to prevent COM garbage collection crashes.
- **Mouse Battery (Linux):** Battery percentage and charging state are monitored via the `org.freedesktop.UPower` D-Bus interface — completely event-driven with **0% idle CPU**. The backend auto-discovers any `Type == 5` (Mouse) device on startup and via hotplug `DeviceAdded`/`DeviceRemoved` signals, so wired/wireless handoffs are handled seamlessly without hardcoded device paths. Requires the `upower` system package (`sudo pacman -S upower` on Arch; `sudo apt install upower` on Debian/Ubuntu). On Windows the old `/tmp/g502_battery.txt` file-read fallback is still supported. Prerequisites: `python-dbus` and `python-gobject` (already required by PyGObject on Linux).
- **Hardcoded Macros:** The "Macros & Apps" buttons (Terminal, Browser, Task Manager) are currently hardcoded for specific Linux apps (e.g., `alacritty`, `brave`, `gnome-system-monitor`) or generic Windows equivalents. You will likely need to adjust these inside the source code if your setup differs.
- **Android Architecture:** The Android application is entirely native Kotlin (utilizing `MainActivity.kt` and an Android `WebView`). Capacitor has been fully removed for improved performance.
- **Security:** There is no authentication layer for the web dashboard itself. Please ensure you are running this exclusively on a trusted, private local network.
- **Troubleshooting:** If the app misbehaves or crashes, check `server.log` (located in the application directory or `%APPDATA%\touch-dashboard` on Windows) for tracebacks.

---

## Running from Source (Linux & Developers)

### Linux (Automated Installation)

Just like Windows, you can install the Linux backend and its dependencies using a one-liner:

```bash
bash -c "$(curl -fsSL https://codeberg.org/liburnb/Touch-Dashboard/raw/branch/main/scripts/setup.sh)" < /dev/tty
```

This interactive script will automatically detect your distribution (Arch, Debian/Ubuntu, or Fedora), install the required dependencies (`python3`, `pipewire`, `playerctl`, etc.), and create a background Desktop shortcut!

### Manual Installation (Developers)

1. Clone the repository: `git clone https://codeberg.org/liburnb/Touch-Dashboard.git`
2. Enter the directory: `cd Touch-Dashboard`
3. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. **Linux specific:** Ensure you have the required PipeWire/WirePlumber tools installed on your system (`wpctl`, `pactl`, `pw-play`, `pw-link`, `playerctl`) as well as `webkit2gtk-4.1`.
   *Arch Linux Users:* You can optionally build and install a local pacman package automatically:
   ```bash
   chmod +x scripts/build-arch-package.sh
   ./scripts/build-arch-package.sh -si
   ```

### Running the Backend

Run `python server.py`. It will start a local FastAPI backend on port `8888` and show a tray icon displaying your IP. 

*Note for Linux Soundboard:* The soundboard feature creates a PipeWire null sink named `Dashboard-Soundboard`, plays sounds into it with `pw-play`, and attempts to route that audio into apps using the microphone. This is highly tailored to specific setups and requires absolute paths to local `.mp3` or `.wav` files.

*Note for Android Developers:* Ensure Java (JDK 17 or 21) and the Android SDK are installed. Run `./scripts/build-android.sh release` to automatically generate the native APK.
