# Touch Dashboard

## Heads Up

This project is a tablet-oriented macro pad and system dashboard, intended as an alternative to tools like Stream Deck or Touch Portal.

The code itself is mostly AI-generated, but the logic is human-reviewed. It currently exists mainly to fit my own workflow and hardware setup. I do plan to make it more modular and generally useful over time, and contributions are welcome from anyone who wants to improve it.

## What It Does

Touch Dashboard runs a local FastAPI server and serves a touch-friendly web interface. Open it from a tablet, phone, or browser and use it as a second-screen command center.

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

The dashboard is currently Linux-focused. Some Windows branches exist in the code, mostly around Soundpad, but this is not currently designed or tested as a Windows-first app.

## Recommended Client Setup

The dashboard is meant to be opened on a tablet. A normal browser works, but on Android I recommend something like the free version of Fully Kiosk Browser for better fullscreen behavior and performance.

Typical flow:

1. Run `server.py` on your main computer.
2. Open `http://YOUR_PC_IP:5000` on the tablet.
3. Keep the tablet on the same local network.
4. Use fullscreen/kiosk mode if possible.

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

Python dependencies used by the current code:

```bash
pip install fastapi uvicorn requests psutil speedtest-cli spotipy pynvml evdev
```

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
git clone git clone https://codeberg.org/liburnb/Touch-Dashboard.git
cd Touch-Dashboard
python -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn requests psutil speedtest-cli spotipy pynvml evdev
```

Run the server:

```bash
python server.py
```

Then open:

```text
http://127.0.0.1:5000
```

From another device on the same network:

```text
http://YOUR_PC_IP:5000
```

The server creates and updates local runtime files such as `config.json`, `server.log`, `weather_cache.json`, and Spotify cache data.

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

## Contributing

Contributions are welcome. Useful areas include:

- Making app buttons and macros configurable.
- Adding a real dependency file.
- Improving setup scripts.
- Better service/plugin modularity.
- More robust Discord client support.
- Better documentation for distro-specific dependencies.
- UI polish and tablet-specific testing.
