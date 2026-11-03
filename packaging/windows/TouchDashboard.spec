# PyInstaller spec for the Windows app: one folder (it starts faster than a
# single exe that unpacks itself on every start, and antivirus flags it less).
#
#   pip install -r requirements-build.txt
#   pyinstaller packaging/windows/TouchDashboard.spec --noconfirm
#
# The page, its assets, the companion addon and version.json go into the
# bundle's _internal folder, where server.py finds them through sys._MEIPASS
# (RESOURCE_DIR) and wow_addons through its own __file__. Then
# packaging/windows/TouchDashboard.iss makes the installer from dist/.
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))   # noqa: F821 (PyInstaller defines SPECPATH)

hidden = (
    collect_submodules("wow_addons")      # its modules are found by pkgutil at run time
    + collect_submodules("uvicorn")       # loops and protocols are imported by name
    + collect_submodules("winrt")         # media controls, window capture, Direct3D, streams
    + ["mouse_battery_win", "soundpad", "lock_monitor", "app_audio", "media", "discord_ipc", "desktop",
       "security", "macro_system", "audio_system"]
)

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "server.py")],
    pathex=[ROOT],
    datas=[
        (os.path.join(ROOT, "templates"), "templates"),
        (os.path.join(ROOT, "static"), "static"),
        (os.path.join(ROOT, "addon"), "addon"),
        (os.path.join(ROOT, "version.json"), "."),
    ],
    hiddenimports=hidden,
    # Linux-only; never imported on Windows
    excludes=["evdev", "gi", "dbus", "mpris_monitor", "mouse_battery"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TouchDashboard",
    icon=os.path.join(ROOT, "static", "favicon.ico"),
    console=False,                        # a window and a tray icon, no console
)
coll = COLLECT(exe, a.binaries, a.datas, name="TouchDashboard")  # noqa: F821
