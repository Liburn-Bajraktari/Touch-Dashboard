# Touch Dashboard

A touch control surface for your PC: an Android tablet (or any browser) becomes a desk console with studio faders, a now-playing deck, Discord voice controls, macro keys and a live system monitor. An open alternative to Stream Deck and Touch Portal, for Linux and Windows.

## Heads Up

This project is a tablet-oriented macro pad and system dashboard, intended as an alternative to tools like Stream Deck or Touch Portal.

The code itself is mostly AI-generated, but the logic is human-reviewed. It currently exists mainly to fit my own workflow and hardware setup. I do plan to make it more modular and generally useful over time, and contributions are welcome from anyone who wants to improve it.

![Touch Dashboard in the Graphite theme: now playing with cover-tinted controls, Discord voice channel, studio faders with an output encoder, macro keys and system monitor](docs/screenshots/dashboard-graphite.png)

![The same dashboard in the Daylight theme: white console, dark displays and silver fader caps](docs/screenshots/dashboard-daylight.png)

<p align="center"><sub>Graphite (the default) and Daylight, two of six themes. Rendered from the app with demo content; the play key and the light around the panels take their colour from the cover that is playing.</sub></p>

---

## Contents

- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Install](#install)
- [First-time setup](#first-time-setup)
- [Using the dashboard](#using-the-dashboard)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Platform notes](#platform-notes)
- [For developers](#for-developers)

## What it does

| Module | What you get |
|---|---|
| **Now playing** | Cover art, title, artist, playing app and track progress, with previous / play-pause / next. Works with Spotify (Web API) or any local player: MPRIS via `playerctl` on Linux, the system media controls on Windows. The cover's colour tints the play key, the progress bar and the light around the panels. |
| **Audio mixer** | Two studio faders for speaker and microphone volume, each with a mute key, and a rotary encoder that switches the output device (one LED per device). |
| **Discord** | Mute, deafen, camera and screen share, the current voice channel, who is in it and who is speaking, and a leave-call key. |
| **Macros & apps** | Nine display keys: launch apps, send keyboard shortcuts, open the soundboard or trigger clip recording. |
| **Soundboard** | Drum pads that play clips into your microphone (Linux, through PipeWire) or through Soundpad / a local folder (Windows). |
| **System** | CPU, RAM and GPU load with a live trace of the last minute, mouse battery, a network speed test and a power-off key. |
| **Clock & weather** | Time, date and current weather from OpenWeatherMap. |
| **Lock screen** | When the PC locks, the tablet shows a large clock with the weather; it shifts slightly every minute to protect OLED screens. |
| **Themes** | Graphite, Daylight, Ember, Phosphor, High contrast and Classic. A choice applies to every connected screen. |

## How it works

Touch Dashboard is client and server:

- **The desktop app** runs on your PC. It reads audio, media, Discord, hardware and battery state, runs your macros, and serves the dashboard on port **8888** over HTTP and WebSocket. It opens a desktop window of the dashboard and lives in the system tray.
- **The Android app** is a full-screen WebView with the dashboard built in. It connects to the PC over your local network.
- **Any browser** on the PC or the network can open the dashboard at `http://<pc-ip>:8888` as well.

The PC and the tablet must be on the same network. There is no login: run it on a trusted, private network only.

## Install

### Windows

1. Open PowerShell and run:
   ```powershell
   irm https://codeberg.org/liburnb/Touch-Dashboard/raw/branch/main/scripts/install.ps1 | iex
   ```
2. A text menu opens. Press **1** to download the app, set up Python and create the Start Menu and desktop shortcuts. The app installs to `%LOCALAPPDATA%\Touch-Dashboard`.
3. Launch **Touch Dashboard** from the Start Menu. It opens the dashboard window and keeps running in the system tray when you close the window (you can turn that off in the window's menu).

The installer also has a repair mode, an uninstaller, and an option (4) to download the latest Android APK to your PC. It uses a PowerShell web install so Windows Smart App Control does not block it.

### Linux

Run:

```bash
bash -c "$(curl -fsSL https://codeberg.org/liburnb/Touch-Dashboard/raw/branch/main/scripts/setup.sh)" < /dev/tty
```

The script detects Arch, Debian/Ubuntu or Fedora, installs the system packages it needs (Python, PipeWire and WirePlumber, `playerctl`, `webkit2gtk-4.1`, `python-dbus`, `python-gobject`, `upower`), installs the app to `~/.local/share/Touch-Dashboard` and adds **Touch Dashboard** to your application menu. On other distributions it lists the packages to install yourself.

### Android

1. Download the latest `TouchDashboard-vX.Y.Z.apk` from the [Releases](https://github.com/Liburn-Bajraktari/Touch-Dashboard/releases) page, or with option 4 of the Windows installer.
2. Copy it to your tablet or phone and install it. You may need to allow installing apps from unknown sources.

## First-time setup

### 1. Start the desktop app and find your IP

Start **Touch Dashboard** on the PC. Its local IP address is shown at the top of the tray icon's menu (right-click it) and in the desktop window's title bar, for example `192.168.1.50`.

### 2. Connect the tablet

1. Open the Touch Dashboard app on the tablet.
2. On first launch it asks for the PC's IP address. Enter it and tap **Connect**.
3. The app remembers the address. The green light next to the title means the tablet is connected.

### 3. Configure the integrations

Settings are edited on the PC and apply to every connected screen:

- **Desktop window:** open the menu button (top left) and choose **Settings**.
- **Browser on the PC:** go to `http://127.0.0.1:8888` and double-click anywhere outside a button.

Click **Save & apply** when you are done (themes apply as soon as you tap them).

| Tab | What to set |
|---|---|
| **General** | Show or hide the mouse battery and the system monitor; turn the Spotify, Weather, Discord, Audio routing and Soundboard modules on or off. |
| **Themes** | Pick one of six presets. Each card previews its theme. |
| **Spotify** | Create an app in the [Spotify developer dashboard](https://developer.spotify.com/dashboard), set its Redirect URI to exactly `http://127.0.0.1:8888/callback`, enter the Client ID and Secret, then click **Connect Spotify**. Do this first sign-in on the PC itself. Without Spotify, local players are still shown. |
| **Weather** | Enter a free [OpenWeatherMap](https://openweathermap.org/) API key and your city, spelled exactly as on its OpenWeather city page. |
| **Discord** | Create an application in the [Discord developer portal](https://discord.com/developers/applications), set its Redirect URI to exactly `http://127.0.0.1:8888/disc_callback`, enter the Client ID and Secret, then click **Authorize Discord**. Discord must be running on the PC. Optional: show the voice channel and sort active speakers to the top. |
| **Audio routing** | Give each output device a short name (up to 10 characters) for the output encoder's display. |
| **Soundboard** | The absolute path of your sounds folder (`.mp3`, `.wav`, `.ogg`, `.flac`, `.m4a`), and optional volume normalisation to a target level (needs `ffmpeg`; 89 dB is the ReplayGain standard Soundpad uses). |

#### Using Vesktop instead of Discord

Vesktop has to have its local IPC server turned on:

1. In Vesktop open **Settings**, scroll to the **Vencord** section and click **Open Settings Folder**.
2. Open `settings.json` (or `settings/settings.json`) in a text editor and make sure both are `true`:
   ```json
   {
     "discordIpc": true,
     "arRPC": true
   }
   ```
3. Restart Vesktop.

If the Discord tab then shows **Vesktop detected**, its arRPC bridge cannot control voice. Use the official Discord app or Vencord for mute and deafen. When voice control is unavailable, the dashboard falls back to the keyboard shortcuts `Ctrl+Shift+M` (mute) and `Ctrl+Shift+D` (deafen).

## Using the dashboard

| To | Do this |
|---|---|
| Change volume | Drag a fader cap, or tap anywhere on its track to jump there. On Android tablets that allow it, you feel a tick every 10 %. |
| Mute | Tap the key under a fader, or pull the fader below 0. |
| Switch output device | Tap the encoder. The lit LED shows which device is active. |
| Control playback | Previous, play/pause and next under the cover. |
| Discord voice | Mute, deafen, camera and screen share keys; the red key next to the channel name leaves the call. |
| Run a macro | Tap its key. |
| Add or edit a macro | Tap **Add**, or press and hold a key for about half a second. A macro can launch an app, send a keyboard shortcut, run a pre-made action (terminal, browser, task manager) or a plugin (soundboard, clip recording). |
| Use the soundboard | Tap the **Soundboard** key. Tap a pad to play it; press and hold to edit it; **Stop audio** stops playback. On Linux the bar at the top sets the soundboard volume and toggles hearing it yourself. |
| Run a speed test | Tap the gauge at the top right. |
| Turn off the PC | Tap the power key: it opens your desktop's shutdown prompt (KDE, GNOME, Xfce, Cinnamon, MATE or Windows). |
| Go full screen (browser) | Double-tap an empty area. |

## Updating

- **Desktop app:** it checks for new releases on start and every 30 minutes, and shows an update prompt in the desktop window and in any browser showing the dashboard. **Update** downloads and installs it in the background, then restarts the app.
- **Android app:** when the desktop app needs a newer Android app, the tablet shows a prompt with a download link to the new APK. Install it over the old one.

## Troubleshooting

- **The tablet cannot connect:** check that both are on the same network, that the IP is the one from the tray menu, and that your firewall allows incoming connections on port 8888.
- **Spotify or Discord will not sign in:** the Redirect URI in their developer portal must match exactly (`http://127.0.0.1:8888/callback`, `http://127.0.0.1:8888/disc_callback`), and the sign-in must happen on the PC.
- **Logs:** `~/.config/touch-dashboard/server.log` on Linux, `%APPDATA%\touch-dashboard\server.log` on Windows. A crash also writes `crash_<time>.txt` next to it, with an error code to quote when reporting it.
- **Settings and data** live in the same folder (`config.json`, the Spotify token cache, sounds).

## Platform notes

- **Linux audio** uses PipeWire and WirePlumber (`wpctl`, `pactl`, `pw-play`, `pw-link`). The soundboard creates a virtual sink called `Dashboard-Soundboard`, plays clips into it and links it into the apps that use your microphone (Discord, OBS and so on). Routing like this depends on your setup and may need adjusting.
- **Mouse battery** on Linux comes from UPower over D-Bus, event-driven, for any mouse UPower knows about (`upower` must be installed). On Windows it reads `%TEMP%\g502_battery.txt`, written by an external script.
- **Windows** integrates with WASAPI (`pycaw`), COM for switching the default device, and WinRT for the media controls.
- **App launchers** follow your desktop environment and the `$TERMINAL` / `$BROWSER` variables instead of fixed program names.
- **The Android app** is a native WebView with the dashboard bundled in. A change to the dashboard reaches tablets with the next APK release.

## For developers

### Run from source

```bash
git clone https://codeberg.org/liburnb/Touch-Dashboard.git
cd Touch-Dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Linux, also install the PipeWire tools (`wpctl`, `pactl`, `pw-play`, `pw-link`), `playerctl` and `webkit2gtk-4.1`.

```bash
python server.py                 # desktop window + tray
python server.py --server-only   # headless server
python server.py --reload        # restart on file changes
python server.py --port 9999     # another port (the Android app always uses 8888)
```

Arch users can build and install a local pacman package with `./scripts/build-arch-package.sh -si`.

### Tests

```bash
python -m pytest tests -v
```

The suite covers the HTTP and WebSocket API, media parsing, and every theme preset's completeness and contrast. The tests start the real server lifespan, which on Linux calls `pactl`. To keep a run off your live audio session, point `HOME` at a scratch folder and put stub `pactl`, `wpctl`, `pw-*` and `playerctl` scripts that exit 1 first on your `PATH`.

### Project layout

| Path | What it is |
|---|---|
| `server.py` | FastAPI app: WebSocket hub, HTTP routes, background polling, startup |
| `desktop.py` | Desktop window (pywebview) and tray icon (pystray) |
| `audio_system.py`, `media.py`, `discord_ipc.py`, `macro_system.py`, `mouse_battery.py`, `lock_monitor.py` | Platform integrations |
| `templates/index.html` | The whole dashboard: markup, styles and script |
| `static/themes.js` | Every design token and the six theme presets |
| `docs/DESIGN.md` | The design system: light model, materials, components, motion and how to add a theme |
| `scripts/` | Installers (`setup.sh`, `install.ps1`), `release.sh`, `build-arch-package.sh` |
| `tests/` | The test suite |

### Releasing

```bash
./scripts/release.sh v1.2.3          # desktop release
./scripts/release.sh v1.2.3 --apk    # also rebuild the Android app
```

The script sets the version in `version.json`, commits everything in the working tree as `Release: <tag>`, tags and pushes, creates the GitHub release (mirrored to Codeberg for older installs). With `--apk` it repacks the newest `TouchDashboard-*.apk` in the repo root with the current `templates/` and `static/`, signs it, uploads it, and raises the required Android app version so older tablets are prompted to update. Signing a fresh Android build is covered in [docs/android-signing.md](docs/android-signing.md).

### References

- [docs/DESIGN.md](docs/DESIGN.md): design rules and tokens.
- [docs/discord_documentation_links.md](docs/discord_documentation_links.md): Discord RPC and IPC documentation used by `discord_ipc.py`.
- Contributions are welcome. Read `docs/DESIGN.md` before changing the interface; `tests/test_themes.py` fails on hard-coded colours.
