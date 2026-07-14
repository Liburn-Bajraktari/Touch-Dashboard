# Touch Dashboard

## Heads Up

This project is a tablet-oriented macro pad and system dashboard, intended as an alternative to tools like Stream Deck or Touch Portal.

The code itself is mostly AI-generated, but the logic is human-reviewed. It currently exists mainly to fit my own workflow and hardware setup. I do plan to make it more modular and generally useful over time, and contributions are welcome from anyone who wants to improve it.

## What It Does

Touch Dashboard runs a local FastAPI server and serves a touch-friendly web interface. Open it from the desktop wrapper, Android app, tablet, phone, or browser and use it as a second-screen command center.

Current features include:

- System stats for CPU, RAM, optional NVIDIA GPU, and optional mouse battery.
- Media display for Spotify or local MPRIS players.
- Spotify playback controls.
- Discord mute/deafen controls through local Discord IPC where supported.
- Speaker and microphone volume sliders through PipeWire/WirePlumber.
- Audio output cycling.
- A local Linux soundboard that plays files through a virtual PipeWire sink.
- Basic app/macro buttons for tools like terminal, browser, task manager, and clipping.
- Weather display through OpenWeatherMap.
- Speedtest trigger.
- Desktop tray mode with a local IP display for connecting mobile clients.
- Android wrapper support through Capacitor.

The dashboard natively supports both Linux and Windows. 
On Windows, it deeply integrates with the OS using native COM, `pycaw` for WASAPI audio manipulation, and WinRT (`Windows.Media.Control`) for capturing Spotify and media players asynchronously with 0% CPU overhead.

## Recommended Client Setup

The dashboard is meant to be opened on a tablet or phone while the backend runs on your main computer. The native Android wrapper is the preferred mobile client, but a normal browser still works.

Typical flow:

1. Run the desktop app or `server.py` on your main computer.
2. Check the tray menu for `Local IP: ...`.
3. Open the Android app, or browse to `http://YOUR_PC_IP:5000` on another device.
4. Keep the tablet on the same local network.

If you use the Android app, it will ask for the desktop IP address the first time. It caches that IP and asks again if the connection fails.

On desktop, `python server.py` starts the FastAPI backend in the background and adds a system tray icon. Use the tray menu to show/hide the dashboard window, read the local IP address, or quit.

## Touch Controls

On a touch device:

- Single-finger double tap toggles fullscreen.
- Two-finger double tap opens the settings menu.
- Drag the speaker or microphone sliders to change volume.
- Tap the speaker or microphone icon at the bottom of a slider to mute or unmute.
- Tap the output device button in the middle of the audio panel to cycle outputs.

On a PC:

- Double click anywhere outside buttons, inputs, and sliders to open settings.
- The volume sliders also work with mouse drag.

Soundboard controls:

- Tap a soundboard button to play that sound.
- Press and hold a soundboard button for about half a second to edit it.
- Tap the plus button to add a new soundboard button.
- Tap the back arrow in the top-left area to leave the soundboard view.

Modal behavior:

- Tap/click outside the settings modal or soundboard edit modal to close it.
- Use Save & Apply in settings to write configuration changes.
- Use Clear Auth in Spotify or Discord settings to remove saved auth state.

Top bar:

- Tap the activity button to run a speedtest.

## Requirements

### Windows

1. Run `py -m pip install -r requirements.txt` to install the dependencies.
2. Run `py server.py` to start the backend.

### Linux

Python dependencies used by the current code:

```bash
pip install -r requirements.txt
```

If a dependency is missing when running from source, the app shows the missing package list and asks before installing anything. In a Linux CLI/server-only run, the same prompt appears in the terminal.

Linux command-line tools used by the backend:

```bash
wpctl
pactl
pw-play
pw-link
playerctl
```

Depending on your distro, these usually come from PipeWire, WirePlumber, PulseAudio compatibility tools, and Playerctl packages.

Optional tools and assumptions:

- NVIDIA GPU stats use `pynvml` and require NVIDIA drivers/NVML.
- App buttons currently assume commands like `alacritty`, `brave`, and `gnome-system-monitor`.
- The mouse battery display reads `/tmp/g502_battery.txt`, which must be created by some external script if you want that value.
- Macro key output uses `evdev`, so your user may need permissions for `/dev/uinput`.

## Setup

Clone the repo and create a virtual environment:

```bash
git clone https://codeberg.org/liburnb/Touch-Dashboard.git
cd Touch-Dashboard
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the desktop app:

```bash
python server.py
```

The app starts in the system tray. Open the tray menu and choose Show Dashboard, or connect from another device using the displayed local IP.

To run only the backend without the desktop window or tray:

```bash
python server.py --server-only --host 0.0.0.0 --port 5000
```

Local browser URL:

```text
http://127.0.0.1:5000
```

From another device on the same network:

```text
http://YOUR_PC_IP:5000
```

The server creates and updates local runtime files such as `config.json`, `server.log`, `weather_cache.json`, and Spotify cache data.

## Native App Builds

### Windows

Build the desktop executable and installer:

```powershell
.\scripts\build-windows.ps1
```

If Inno Setup is not installed, you can still build only the executable:

```powershell
.\scripts\build-windows.ps1 -SkipInstaller
```

### Arch Linux

Build and install a local package:

```bash
chmod +x scripts/build-arch-package.sh
./scripts/build-arch-package.sh -si
```

### Android

Build a debug APK:

```bash
chmod +x scripts/build-android.sh
./scripts/build-android.sh debug
```

Build a release APK:

```bash
./scripts/build-android.sh release
```

The Android app is a client only. The backend must still be running on your desktop or Linux host on the same Wi-Fi network.

## First-Time Configuration

Open the dashboard, then open settings with a double click or the configured touch gesture. The settings modal currently includes tabs for Spotify, Weather, Discord, Audio Routing, and Soundboard.

### Spotify

Create a Spotify Developer app and enter the Client ID and Client Secret in settings.

Use this redirect URI in the Spotify app settings:

```text
http://127.0.0.1:5000/callback
```

Important: Spotify authentication must be completed from the same local machine that is running `server.py`.

### Discord

Create a Discord Developer Application and enter the Client ID and Client Secret in settings.

Use this redirect URI:

```text
http://127.0.0.1:5000/disc_callback
```

Discord control depends on local Discord IPC, so Discord or a compatible client must be running on the same machine as `server.py`.

Some clients, such as Vesktop/arRPC setups, may expose IPC but not support the voice control commands used here. In those cases the UI may show a warning and voice controls may not work as expected.

### Weather

Enter an OpenWeatherMap API key and city name in settings.

### Audio Routing

The dashboard discovers PipeWire/WirePlumber output devices through `wpctl`. You can assign short custom names for display on the audio output button.

### Soundboard

On Linux, set the absolute path to your local sounds directory. Supported file types are currently:

```text
.mp3
.wav
.ogg
```

The server creates a PipeWire null sink named `Dashboard-Soundboard`, plays sounds into it with `pw-play`, and attempts to route that audio into apps that are using the microphone.

This system is still tailored to my setup and will likely become more modular later.

## Current UI Sections

### Now Playing

Shows Spotify metadata when Spotify is active. If Spotify is not playing, the backend falls back to local MPRIS media through `playerctl`.

### Discord

Shows mute/deafen controls only when Discord IPC is connected. Camera and screen buttons are placeholders right now because the current IPC flow does not implement those actions.

### Audio

Provides speaker and microphone volume sliders and an output-device cycle button.

### Macros & Apps

These buttons are currently hard-coded. For example, terminal/browser/task manager launch specific Linux commands. Expect this section to become more configurable in the future.

### Soundboard

The soundboard page lets you add buttons for local audio files. Tap to play. Long press to edit.

### System Monitor

Displays CPU and RAM. GPU appears only when NVML is available.

## Notes and Limitations

- This is not yet a polished general-purpose product.
- Linux/PipeWire is the main target right now.
- Some app buttons and paths are hard-coded.
- Some features assume specific hardware or external scripts.
- Service authentication must happen on the server machine.
- Keep this on a trusted local network. There is no authentication layer for the dashboard itself yet.
- The Android app expects direct LAN access to the desktop server at port `5000`.

## Contributing

Contributions are welcome. Useful areas include:

- Making app buttons and macros configurable.
- Improving setup scripts.
- Better service/plugin modularity.
- More robust Discord client support.
- Better documentation for distro-specific dependencies.
- UI polish and tablet-specific testing.
