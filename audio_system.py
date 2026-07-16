"""
audio_system.py — Cross-platform audio management for Touch Dashboard.

Linux:   PipeWire / WirePlumber via wpctl + pactl + pw-play
Windows: Windows Core Audio (MMDevice / WASAPI) via pycaw
         Default-device cycling via IPolicyConfig raw-COM vtable call
         Soundboard via sounddevice + soundfile
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time

# ─── Monkeypatch pycaw to prevent Access Violations ───────────────────────────
# pycaw's AudioUtilities.CreateDevice calls `value.clear()` on PROPVARIANTs.
# comtypes's GC also tries to release them. This double-free causes 0xC0000005
# crashes on Windows randomly during audio polling. We don't need properties!
if sys.platform.startswith("win"):
    try:
        from pycaw.pycaw import AudioUtilities, AudioDeviceState, AudioDevice  # type: ignore[import]
        from comtypes import COMError  # type: ignore[import]
        
        def _safe_create_device(dev):
            if dev is None: return None
            properties = {}
            try:
                from pycaw.constants import STGM
                store = dev.OpenPropertyStore(STGM.STGM_READ.value)
                if store is not None:
                    for j in range(store.GetCount()):
                        pk = store.GetAt(j)
                        key_str = str(pk)
                        if 'a45c254e-df1c-4efd-8020-67d146a850e0' in key_str.lower():
                            try:
                                v = store.GetValue(pk).GetValue()
                                properties[key_str] = v
                            except COMError:
                                pass
            except Exception:
                pass
            return AudioDevice(dev.GetId(), AudioDeviceState(dev.GetState()), properties, dev)
            
        AudioUtilities.CreateDevice = _safe_create_device
    except Exception:
        pass

logger = logging.getLogger(__name__)

# ─── COM apartment management (Windows only) ──────────────────────────────────

_com_tls = threading.local()

def _com_cleanup(func):
    """Decorator to force garbage collection of COM objects on the initialized thread."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        finally:
            import gc
            gc.collect(0)
    return wrapper

def _ensure_com() -> None:
    """
    Initialize COM in Multi-Threaded Apartment (MTA) mode for the calling thread.
    MTA is CRITICAL for Python because the Garbage Collector can run on ANY thread.
    If we use STA, GC'ing a comtypes object on the wrong thread causes 0xC0000005 access violations.
    """
    if not sys.platform.startswith("win"):
        return
    if getattr(_com_tls, "initialized", False):
        return
    try:
        # 0 = COINIT_MULTITHREADED
        sys.coinit_flags = 0
        import comtypes  # type: ignore[import]
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        _com_tls.initialized = True
    except Exception as e:
        logger.debug(f"COM CoInitializeEx: {e}")



# ─── IPolicyConfig: undocumented Windows COM for default-device switching ─────
#   Same technique used by EarTrumpet, SoundSwitch, AudioEndPointController.
#   CLSID: {870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}
#   IID:   {F8679F50-850A-41CF-9C72-430F290290C8}
#   vtable layout (0-indexed, includes IUnknown's QueryInterface/AddRef/Release):
#     [0] QueryInterface  [1] AddRef  [2] Release
#     [3] GetMixFormat    [4] GetDeviceFormat  [5] ResetDeviceFormat
#     [6] SetDeviceFormat [7] GetProcessingPeriod [8] SetProcessingPeriod
#     [9] GetShareMode    [10] SetShareMode
#     [11] GetPropertyValue [12] SetPropertyValue
#     [13] SetDefaultEndpoint  ← the one we need
#     [14] SetEndpointVisibility

def _windows_set_default_audio_device(device_id: str) -> bool:
    """
    Set the Windows default audio output device for all three roles
    (eConsole=0, eMultimedia=1, eCommunications=2) via raw COM vtable.
    Returns True on success.

    IMPORTANT: This function initialises and uninitialises COM itself so it
    is safe to call from ANY thread (threadpool, asyncio.to_thread, etc.).
    Using CLSCTX_LOCAL_SERVER avoids in-process apartment restrictions.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        import ctypes.wintypes as wt

        ole32 = ctypes.windll.ole32

        # ── Initialise COM for this thread (STA) ──────────────────────────────
        # COINIT_APARTMENTTHREADED = 0x2
        hr_init = ole32.CoInitializeEx(None, 0x2)
        com_init_ok = hr_init in (0, 1)  # S_OK or S_FALSE (already initialised)

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wt.DWORD),
                ("Data2", wt.WORD),
                ("Data3", wt.WORD),
                ("Data4", ctypes.c_byte * 8),
            ]

        def _make_guid(s: str) -> _GUID:
            s = s.strip("{}").replace("-", "")
            d4_bytes = bytes.fromhex(s[16:32])
            return _GUID(
                int(s[0:8], 16),
                int(s[8:12], 16),
                int(s[12:16], 16),
                (ctypes.c_byte * 8)(*d4_bytes),
            )

        clsid = _make_guid("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")
        iid   = _make_guid("{F8679F50-850A-41CF-9C72-430F290290C8}")

        iface_ptr = ctypes.c_void_p()

        # CLSCTX_LOCAL_SERVER (0x4) avoids cross-apartment in-proc issues.
        # Fall back to CLSCTX_ALL (0x17) if local server fails.
        result = False
        for clsctx in (0x4, 0x17, 0x1):  # LOCAL_SERVER, ALL, INPROC_SERVER
            hr = ole32.CoCreateInstance(
                ctypes.byref(clsid), None, clsctx,
                ctypes.byref(iid), ctypes.byref(iface_ptr),
            )
            if hr == 0 and iface_ptr.value:
                break
        else:
            logger.debug(f"IPolicyConfig CoCreateInstance hr=0x{hr & 0xFFFFFFFF:08x}")
            if com_init_ok and hr_init == 0:
                ole32.CoUninitialize()
            return False

        try:
            # Navigate vtable: index 13 = SetDefaultEndpoint
            vtbl_ptr = ctypes.cast(iface_ptr, ctypes.POINTER(ctypes.c_void_p))
            if not vtbl_ptr[0]:   # vtable base is NULL — corrupted COM object
                logger.debug("IPolicyConfig vtable pointer is NULL")
                return False

            vtable = ctypes.cast(vtbl_ptr[0], ctypes.POINTER(ctypes.c_void_p))

            _SetDefaultEndpoint = ctypes.WINFUNCTYPE(
                ctypes.c_long,    # HRESULT
                ctypes.c_void_p,  # this
                ctypes.c_wchar_p, # pwstrDeviceId
                ctypes.c_uint,    # ERole
            )(vtable[13])

            this = iface_ptr.value
            for role in range(3):
                _hr = _SetDefaultEndpoint(this, device_id, role)
                if _hr not in (0, 1):  # S_OK or S_FALSE
                    logger.debug(f"SetDefaultEndpoint role={role} hr=0x{_hr & 0xFFFFFFFF:08x}")

            result = True
        finally:
            # Always Release the COM object, even on error
            try:
                vtbl_ptr2 = ctypes.cast(iface_ptr, ctypes.POINTER(ctypes.c_void_p))
                vtable2   = ctypes.cast(vtbl_ptr2[0], ctypes.POINTER(ctypes.c_void_p))
                _Release  = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable2[2])
                _Release(iface_ptr.value)
            except Exception as rel_err:
                logger.debug(f"IPolicyConfig Release: {rel_err}")
            # Uninitialise COM only if WE initialised it (hr_init == 0)
            if com_init_ok and hr_init == 0:
                ole32.CoUninitialize()

        return result

    except Exception as e:
        logger.error(f"Windows SetDefaultEndpoint failed: {e}")
        return False


# ─── Windows: pycaw-based volume helpers ──────────────────────────────────────

def _pycaw_endpoint(is_mic: bool):
    """Return the pycaw IMMDevice for speaker or microphone."""
    from pycaw.pycaw import AudioUtilities  # type: ignore[import]
    return AudioUtilities.GetMicrophone() if is_mic else AudioUtilities.GetSpeakers()


@_com_cleanup
def _windows_get_volume(target: str) -> dict:
    """Return {vol: int 0-100, muted: bool} for the given Windows endpoint."""
    try:
        _ensure_com()
        from pycaw.pycaw import IAudioEndpointVolume  # type: ignore[import]
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL  # type: ignore[import]

        is_mic = (target == "@DEFAULT_AUDIO_SOURCE@")
        device = _pycaw_endpoint(is_mic)
        if not device:
            return {"vol": 0, "muted": False}

        if is_mic:
            interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
        else:
            volume = device.EndpointVolume
            
        vol = round(volume.GetMasterVolumeLevelScalar() * 100)
        muted = bool(volume.GetMute())
        return {"vol": vol, "muted": muted}
    except Exception as e:
        logger.debug(f"Windows get_volume {target}: {e}")
        return {"vol": 0, "muted": False}


@_com_cleanup
def _windows_set_volume(target: str, val: int) -> None:
    """Set volume (0–100) for the given Windows endpoint."""
    try:
        _ensure_com()
        from pycaw.pycaw import IAudioEndpointVolume  # type: ignore[import]
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL  # type: ignore[import]

        is_mic = (target == "@DEFAULT_AUDIO_SOURCE@")
        device = _pycaw_endpoint(is_mic)
        if not device:
            return

        if is_mic:
            interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
        else:
            volume = device.EndpointVolume
            
        volume.SetMasterVolumeLevelScalar(max(0.0, min(1.0, val / 100.0)), None)
        volume.SetMute(0, None)
    except Exception as e:
        logger.debug(f"Windows set_volume {target}={val}: {e}")


@_com_cleanup
def _windows_toggle_mute(target: str, macro_send_keys_fn=None) -> None:
    """Toggle mute for the given Windows endpoint."""
    try:
        _ensure_com()
        if target == "@DEFAULT_AUDIO_SINK@":
            # Speaker: use media key (avoids COM re-entrancy on the same object)
            if macro_send_keys_fn:
                macro_send_keys_fn("KEY_MUTE")
            return
        is_mic = (target == "@DEFAULT_AUDIO_SOURCE@")
        device = _pycaw_endpoint(is_mic)
        if not device:
            return

        if is_mic:
            interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
        else:
            volume = device.EndpointVolume
            
        volume.SetMute(not bool(volume.GetMute()), None)
    except Exception as e:
        logger.debug(f"Windows toggle_mute {target}: {e}")


@_com_cleanup
def _windows_enumerate_endpoints(audio_names: dict) -> dict:
    """
    Enumerate Windows render (output) devices using pycaw.
    Returns a dict in the same shape as AudioSystem.poll_all().

    pycaw 2025 API notes:
      - Use device.id  (not .GetId())
      - Render (output) device IDs start with '{0.0.0.'
      - Capture (input) device IDs start with '{0.0.1.'
      - Use device.state == AudioDeviceState.Active to filter live endpoints
    """
    try:
        _ensure_com()
        from pycaw.pycaw import AudioUtilities  # type: ignore[import]
        try:
            from pycaw.pycaw import AudioDeviceState  # type: ignore[import]
            ACTIVE = AudioDeviceState.Active
        except ImportError:
            # Older pycaw — fall back to string comparison
            ACTIVE = None  # type: ignore[assignment]

        devices = AudioUtilities.GetAllDevices()
        active_device = AudioUtilities.GetSpeakers()
        active_id = active_device.id if active_device else None

        sinks: list[dict] = []
        for d in devices:
            dev_id: str = d.id or ""
            # Render (output) endpoints have IDs starting with {0.0.0.
            # Capture (input) endpoints start with {0.0.1.
            if not dev_id.startswith("{0.0.0."):
                continue
            # Only show active (plugged-in + enabled) devices
            state = getattr(d, "state", None)
            if ACTIVE is not None and state != ACTIVE:
                continue
            elif ACTIVE is None and str(state) not in ("Active", "1"):
                continue

            raw_name = d.FriendlyName or "Unknown"
            custom_name = audio_names.get(raw_name, "")
            display_name = custom_name[:10] if custom_name else raw_name[:8].upper()
            is_active = (dev_id == active_id)
            sinks.append({
                "id": dev_id,
                "name": display_name,
                "raw_name": raw_name,
                "custom_name": custom_name,
                "is_active": is_active,
            })

        spk = _windows_get_volume("@DEFAULT_AUDIO_SINK@")
        mic = _windows_get_volume("@DEFAULT_AUDIO_SOURCE@")
        active_name = next((s["name"] for s in sinks if s["is_active"]), "NONE")

        return {
            "sinks": sinks,
            "active_sink_name": active_name,
            "spk": spk,
            "mic": mic,
        }
    except Exception as e:
        logger.error(f"Windows endpoint enum: {e}")
        return {
            "sinks": [{"id": "default", "name": "Default", "raw_name": "Windows Default",
                        "custom_name": "", "is_active": True}],
            "active_sink_name": "Default",
            "spk": {"vol": 50, "muted": False},
            "mic": {"vol": 50, "muted": False},
        }


# ─── Windows soundboard ────────────────────────────────────────────────────────

_win_sound_thread: threading.Thread | None = None


def _windows_play_sound(filepath: str) -> None:
    """Play an audio file on Windows using sounddevice + soundfile (non-blocking)."""
    global _win_sound_thread
    _windows_stop_sound()

    def _play() -> None:
        try:
            import sounddevice as sd    # type: ignore[import]
            import soundfile as sf      # type: ignore[import]
            data, fs = sf.read(filepath, dtype="float32", always_2d=True)
            sd.play(data, fs)
            sd.wait()
        except Exception as e:
            logger.debug(f"Windows soundboard play '{filepath}': {e}")

    _win_sound_thread = threading.Thread(target=_play, daemon=True, name="win-sound")
    _win_sound_thread.start()


def _windows_stop_sound() -> None:
    global _win_sound_thread
    try:
        import sounddevice as sd   # type: ignore[import]
        sd.stop()
    except Exception:
        pass
    if _win_sound_thread and _win_sound_thread.is_alive():
        _win_sound_thread.join(timeout=0.5)
    _win_sound_thread = None


# ─── Main AudioSystem ──────────────────────────────────────────────────────────

class AudioSystem:
    """
    Cross-platform audio management.
    Platform detection happens inside each method so the same API is used
    everywhere; no caller needs to check the OS.
    """

    _last_state: dict[str, dict] = {}

    _vol_targets: dict[str, int] = {}
    _vol_lock    = threading.Lock()
    _vol_thread: threading.Thread | None = None

    # Windows device list cache (5-second TTL — avoids hammering COM on every poll)
    _win_sinks_cache: list[dict] = []
    _win_sinks_ts: float = 0.0

    # Linux: currently running pw-play process
    _linux_proc: subprocess.Popen | None = None

    # ── subprocess helper ──────────────────────────────────────────────────────

    @staticmethod
    def run(cmd: list[str]) -> str:
        try:
            return subprocess.check_output(
                cmd, stderr=subprocess.DEVNULL, timeout=2
            ).decode().strip()
        except Exception as e:
            logger.debug(f"AudioSystem.run {cmd}: {e}")
            return ""

    # ── volume-worker thread ───────────────────────────────────────────────────

    @staticmethod
    def _vol_worker() -> None:
        """
        Background thread that drains the vol-target queue.
        COM is initialized exactly once for this thread on Windows.
        """
        _ensure_com()
        while True:
            with AudioSystem._vol_lock:
                tasks = list(AudioSystem._vol_targets.items())
                AudioSystem._vol_targets.clear()
            if not tasks:
                time.sleep(0.04)
                continue
            for target, val in tasks:
                if sys.platform.startswith("win"):
                    _windows_set_volume(target, val)
                else:
                    AudioSystem.run(["wpctl", "set-volume", target, f"{val}%"])
                    AudioSystem.run(["wpctl", "set-mute", target, "0"])

    @staticmethod
    def start_worker() -> None:
        if AudioSystem._vol_thread is None or not AudioSystem._vol_thread.is_alive():
            AudioSystem._vol_thread = threading.Thread(
                target=AudioSystem._vol_worker, daemon=True, name="audio-vol-worker"
            )
            AudioSystem._vol_thread.start()

    @staticmethod
    def set_vol(target: str, val: int) -> None:
        AudioSystem.start_worker()
        with AudioSystem._vol_lock:
            AudioSystem._vol_targets[target] = val

    # ── state queries ──────────────────────────────────────────────────────────

    @staticmethod
    def get_state(target: str) -> dict:
        if sys.platform.startswith("win"):
            state = _windows_get_volume(target)
        else:
            out = AudioSystem.run(["wpctl", "get-volume", target])
            if not out:
                return AudioSystem._last_state.get(target, {"vol": 0, "muted": False})
            try:
                parts = out.split()
                vol = int(float(parts[1]) * 100) if len(parts) > 1 else 0
            except Exception:
                vol = 0
            state = {"vol": vol, "muted": "[MUTED]" in out}
        AudioSystem._last_state[target] = state
        return state

    @staticmethod
    def is_muted(target: str) -> bool:
        return bool(AudioSystem.get_state(target).get("muted", False))

    # ── poll_all ───────────────────────────────────────────────────────────────

    @staticmethod
    def poll_all(audio_names: dict | None = None) -> dict:
        """
        Return a snapshot of all audio endpoints, volumes, and mute states.
        Shape: {sinks, active_sink_name, spk: {vol, muted}, mic: {vol, muted}}
        """
        if audio_names is None:
            audio_names = {}

        if sys.platform.startswith("win"):
            now = time.monotonic()
            # Full device scan every 5 s; between scans only refresh vol/mute.
            if (now - AudioSystem._win_sinks_ts) >= 5.0 or not AudioSystem._win_sinks_cache:
                result = _windows_enumerate_endpoints(audio_names)
                AudioSystem._win_sinks_cache = result.get("sinks", [])
                AudioSystem._win_sinks_ts = now
                return result

            spk = _windows_get_volume("@DEFAULT_AUDIO_SINK@")
            mic = _windows_get_volume("@DEFAULT_AUDIO_SOURCE@")
            active_name = next(
                (s["name"] for s in AudioSystem._win_sinks_cache if s["is_active"]), "NONE"
            )
            return {
                "sinks": AudioSystem._win_sinks_cache,
                "active_sink_name": active_name,
                "spk": spk,
                "mic": mic,
            }

        # ── Linux ──────────────────────────────────────────────────────────────
        sinks: list[dict] = []
        active_sink_name = "NONE"
        spk_vol, spk_muted = 0, False
        mic_vol, mic_muted = 0, False
        try:
            out = AudioSystem.run(["wpctl", "status"])
            section: str | None = None
            for line in out.splitlines():
                if "Sinks:" in line:
                    section = "sinks"; continue
                elif "Sources:" in line:
                    section = "sources"; continue
                elif any(x in line for x in ("Filters:", "Streams:", "Video:", "Devices:")):
                    section = None; continue

                if section in ("sinks", "sources"):
                    clean = line.translate(str.maketrans("", "", "│├└─")).strip()
                    if not clean:
                        continue
                    m = re.search(
                        r"^(\*)?\s*(\d+)\.\s+(.+?)\s+\[vol:\s*([\d.]+)(.*)\]", clean
                    )
                    if not m:
                        continue
                    is_active = bool(m.group(1))
                    dev_id    = m.group(2)
                    raw_name  = m.group(3).strip()
                    vol       = int(float(m.group(4)) * 100)
                    is_muted  = "MUTED" in m.group(5)

                    if is_active:
                        if section == "sinks":
                            spk_vol, spk_muted = vol, is_muted
                        else:
                            mic_vol, mic_muted = vol, is_muted

                    if section == "sinks":
                        if "Dashboard-Soundboard" in raw_name:
                            continue
                        custom_name  = audio_names.get(raw_name, "")
                        display_name = custom_name[:10] if custom_name else raw_name[:5].upper()
                        sinks.append({
                            "id": dev_id, "name": display_name,
                            "raw_name": raw_name, "custom_name": custom_name,
                            "is_active": is_active,
                        })
                        if is_active:
                            active_sink_name = display_name
        except Exception as e:
            logger.error(f"Audio poll_all: {e}")

        return {
            "sinks": sinks,
            "active_sink_name": active_sink_name if sinks else "NONE",
            "spk": {"vol": spk_vol, "muted": spk_muted},
            "mic": {"vol": mic_vol, "muted": mic_muted},
        }

    @staticmethod
    def get_hardware_sinks(audio_names: dict | None = None) -> list[dict]:
        return AudioSystem.poll_all(audio_names).get("sinks", [])

    @staticmethod
    def get_active_sink_id(
        sinks: list[dict] | None = None,
        audio_names: dict | None = None,
    ) -> str | None:
        if sinks is None:
            sinks = AudioSystem.get_hardware_sinks(audio_names)
        for s in sinks:
            if s.get("is_active"):
                return s["id"]
        return None

    # ── mute ───────────────────────────────────────────────────────────────────

    @staticmethod
    def toggle_mute(target: str, macro_send_keys_fn=None) -> None:
        if sys.platform.startswith("win"):
            _windows_toggle_mute(target, macro_send_keys_fn)
        else:
            AudioSystem.run(["wpctl", "set-mute", target, "toggle"])

    # ── cycle output device ────────────────────────────────────────────────────

    @staticmethod
    def cycle_device(audio_names: dict | None = None) -> None:
        sinks = AudioSystem.poll_all(audio_names).get("sinks", [])
        if not sinks:
            return

        active_id = AudioSystem.get_active_sink_id(sinks)
        next_sink = sinks[0]
        if active_id:
            for i, s in enumerate(sinks):
                if s["id"] == active_id:
                    next_sink = sinks[(i + 1) % len(sinks)]
                    break

        if sys.platform.startswith("win"):
            if _windows_set_default_audio_device(str(next_sink["id"])):
                # Invalidate cache so the next poll reflects the new default
                AudioSystem._win_sinks_ts = 0.0
        else:
            AudioSystem.run(["wpctl", "set-default", str(next_sink["id"])])

    # ── soundboard ─────────────────────────────────────────────────────────────

    @staticmethod
    def play_local_sound(filepath: str) -> None:
        if sys.platform.startswith("win"):
            _windows_play_sound(filepath)
        else:
            # Terminate any playing sound first
            proc = AudioSystem._linux_proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
            AudioSystem._linux_proc = subprocess.Popen(
                ["pw-play", "--volume=1.0", "--target", "Dashboard-Soundboard", filepath],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    @staticmethod
    def stop_local_sound() -> None:
        if sys.platform.startswith("win"):
            _windows_stop_sound()
        else:
            proc = AudioSystem._linux_proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
                AudioSystem._linux_proc = None
