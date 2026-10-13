"""
linux-iprojection - Virtual monitor for "Extend" mode on X11 via evdi

On Wayland, Mutter creates virtual monitors through the ScreenCast portal.
X11 has no such API, so we use the evdi kernel module (the one behind
DisplayLink adapters): it exposes a fake DRM connector, and once we
"plug" a monitor into it (by handing it an EDID) X11 treats it as a real
display. GNOME extends the desktop onto it and we capture that region
with ximagesrc.

Requirements: evdi-dkms (module loaded, signed for Secure Boot) and
libevdi1. The user needs rw access to the evdi /dev/dri/cardN node
(logind grants it to the active seat).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import logging
import os
import re
import select
import subprocess
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

WIDTH, HEIGHT, REFRESH = 1920, 1080, 60


def _edid_1080p(name: str = "iProjection") -> bytes:
    """Build a 128-byte EDID 1.4 block for a 1920x1080@60 display."""
    e = bytearray(128)
    e[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    e[8:10] = b"\x31\x30"  # manufacturer "LIP"
    e[10:12] = b"\x01\x00"  # product code
    e[16], e[17] = 1, 2026 - 1990  # week, year
    e[18], e[19] = 1, 4  # EDID 1.4
    e[20] = 0x80  # digital input
    e[21], e[22] = 53, 30  # size in cm
    e[23] = 0x78  # gamma 2.2
    e[24] = 0x06  # sRGB default, preferred timing is native
    e[25:35] = bytes.fromhex("ee91a3544c99260f5054")  # sRGB chromaticity
    e[38:54] = b"\x01\x01" * 8  # no standard timings
    # Detailed timing: CEA 1080p60 (148.5 MHz)
    e[54:72] = bytes.fromhex("023a801871382d40582c450013 2a2100001e".replace(" ", ""))
    # Monitor name
    label = name.encode()[:12] + b"\n"
    e[72:90] = b"\x00\x00\x00\xfc\x00" + label.ljust(13, b" ")
    # Range limits: 56-75 Hz, 30-83 kHz, 170 MHz
    e[90:108] = bytes.fromhex("000000fd00384b1e5311000a202020202020")
    # Dummy descriptor
    e[108:126] = b"\x00\x00\x00\x10\x00" + b"\x00" * 13
    e[126] = 0  # no extensions
    e[127] = (-sum(e[:127])) & 0xFF
    return bytes(e)


class _EvdiMode(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int),
                ("refresh_rate", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
                ("pixel_format", ctypes.c_uint)]


class _EvdiRect(ctypes.Structure):
    _fields_ = [("x1", ctypes.c_int), ("y1", ctypes.c_int),
                ("x2", ctypes.c_int), ("y2", ctypes.c_int)]


class _EvdiBuffer(ctypes.Structure):
    _fields_ = [("id", ctypes.c_int), ("buffer", ctypes.c_void_p),
                ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("stride", ctypes.c_int), ("rects", ctypes.POINTER(_EvdiRect)),
                ("rect_count", ctypes.c_int)]


_MODE_CB = ctypes.CFUNCTYPE(None, _EvdiMode, ctypes.c_void_p)
_UPDATE_CB = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)


class _EvdiEventContext(ctypes.Structure):
    _fields_ = [("dpms_handler", ctypes.c_void_p),
                ("mode_changed_handler", _MODE_CB),
                ("update_ready_handler", _UPDATE_CB),
                ("crtc_state_handler", ctypes.c_void_p),
                ("cursor_set_handler", ctypes.c_void_p),
                ("cursor_move_handler", ctypes.c_void_p),
                ("ddcci_data_handler", ctypes.c_void_p),
                ("user_data", ctypes.c_void_p)]


_MAX_RECTS = 16
_BUFFER_ID = 1


def _load_libevdi():
    path = ctypes.util.find_library("evdi")
    candidates = [path] if path else []
    candidates += sorted(glob.glob("/usr/lib/libevdi.so*")) + sorted(
        glob.glob("/usr/lib/*/libevdi.so*")
    )
    for c in candidates:
        try:
            lib = ctypes.CDLL(c)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("libevdi not found (install libevdi1)")
    lib.evdi_open.argtypes = [ctypes.c_int]
    lib.evdi_open.restype = ctypes.c_void_p
    lib.evdi_check_device.argtypes = [ctypes.c_int]
    lib.evdi_check_device.restype = ctypes.c_int
    lib.evdi_close.argtypes = [ctypes.c_void_p]
    lib.evdi_disconnect.argtypes = [ctypes.c_void_p]
    lib.evdi_get_event_ready.argtypes = [ctypes.c_void_p]
    lib.evdi_get_event_ready.restype = ctypes.c_int
    lib.evdi_handle_events.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.evdi_register_buffer.argtypes = [ctypes.c_void_p, _EvdiBuffer]
    lib.evdi_unregister_buffer.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.evdi_request_update.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.evdi_request_update.restype = ctypes.c_bool
    lib.evdi_grab_pixels.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_EvdiRect), ctypes.POINTER(ctypes.c_int),
    ]
    lib.evdi_connect2.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint32, ctypes.c_uint32,
    ]
    return lib


def _find_evdi_card() -> int:
    for path in sorted(glob.glob("/sys/devices/platform/evdi.*/drm/card*")):
        return int(os.path.basename(path)[4:])
    raise RuntimeError(
        "No evdi device found (is the evdi module loaded with initial_device_count=1?)"
    )


@dataclass
class Region:
    output: str
    x: int
    y: int
    width: int
    height: int


def _xrandr(*args: str) -> str:
    return subprocess.run(
        ["xrandr", *args], capture_output=True, text=True, check=False
    ).stdout


def _evdi_output() -> tuple[str, bool, Region | None]:
    """Return (name, connected, active region) of the evdi xrandr output."""
    providers = _xrandr("--listproviders")
    out = _xrandr("--query")
    # evdi outputs are named like DVI-I-1-1 (provider index suffix)
    for line in out.splitlines():
        m = re.match(r"^(DVI-I-\d+-\d+) (connected|disconnected)(?: primary)? ?(\d+)?x?(\d+)?\+?(\d+)?\+?(\d+)?", line)
        if m and "modesetting" in providers:
            name, state = m.group(1), m.group(2)
            region = None
            if m.group(3):
                region = Region(name, int(m.group(5)), int(m.group(6)), int(m.group(3)), int(m.group(4)))
            return name, state == "connected", region
    raise RuntimeError("evdi output not visible in xrandr")


def _primary_output() -> str:
    for line in _xrandr("--query").splitlines():
        if " connected primary" in line:
            return line.split()[0]
    for line in _xrandr("--query").splitlines():
        if " connected" in line and not line.startswith("DVI-I-"):
            return line.split()[0]
    raise RuntimeError("no primary output found")


class EvdiVirtualDisplay:
    """A 1080p virtual monitor placed to the right of the primary display."""

    def __init__(self) -> None:
        self._lib = None
        self._handle = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._buf_mem = None
        self._buf_registered = False
        self._rects = (_EvdiRect * _MAX_RECTS)()
        self._mode_cb = _MODE_CB(self._on_mode_changed)
        self._update_cb = _UPDATE_CB(self._on_update_ready)
        self._ctx = _EvdiEventContext(mode_changed_handler=self._mode_cb,
                                      update_ready_handler=self._update_cb)
        self.region: Region | None = None

    @staticmethod
    def available() -> bool:
        try:
            _find_evdi_card()
            _load_libevdi()
            return True
        except Exception:
            return False

    def start(self, timeout: float = 8.0) -> Region:
        self._lib = _load_libevdi()
        card = _find_evdi_card()
        status = self._lib.evdi_check_device(card)
        if status != 0:  # 0 = AVAILABLE
            raise RuntimeError(f"evdi card{card} not available (status {status})")
        self._handle = self._lib.evdi_open(card)
        if not self._handle:
            raise RuntimeError(f"cannot open /dev/dri/card{card}")

        edid = _edid_1080p()
        self._lib.evdi_connect2(
            self._handle, edid, len(edid), WIDTH * HEIGHT, WIDTH * HEIGHT * REFRESH
        )
        log.info("evdi virtual monitor plugged on card%d", card)

        self._thread = threading.Thread(target=self._event_loop, daemon=True)
        self._thread.start()

        # Wait for X11 to see it, then extend the desktop onto it
        deadline = time.monotonic() + timeout
        name = None
        while time.monotonic() < deadline:
            name, connected, region = _evdi_output()
            if connected:
                break
            time.sleep(0.25)
        else:
            self.stop()
            raise RuntimeError("X11 did not detect the virtual monitor")

        time.sleep(1.0)  # give GNOME a moment to auto-extend on hotplug
        if _evdi_output()[2] is None:
            primary = _primary_output()
            _xrandr("--output", name, "--mode", f"{WIDTH}x{HEIGHT}", "--right-of", primary)

        while time.monotonic() < deadline:
            _, _, region = _evdi_output()
            if region:
                self.region = region
                log.info("Virtual monitor %s at %dx%d+%d+%d", region.output,
                         region.width, region.height, region.x, region.y)
                return region
            time.sleep(0.25)
        self.stop()
        raise RuntimeError("virtual monitor did not become active")

    # evdi only completes a page flip once userspace grabs the frame, so we
    # keep a buffer registered and acknowledge every update (like the
    # DisplayLink manager does). Frames themselves are captured via X11.
    def _on_mode_changed(self, mode: _EvdiMode, _user) -> None:
        log.info("evdi mode %dx%d@%d", mode.width, mode.height, mode.refresh_rate)
        if self._buf_registered:
            self._lib.evdi_unregister_buffer(self._handle, _BUFFER_ID)
            self._buf_registered = False
        if mode.width <= 0 or mode.height <= 0:
            return
        stride = mode.width * 4
        self._buf_mem = ctypes.create_string_buffer(stride * mode.height)
        buf = _EvdiBuffer(_BUFFER_ID, ctypes.cast(self._buf_mem, ctypes.c_void_p),
                          mode.width, mode.height, stride,
                          ctypes.cast(self._rects, ctypes.POINTER(_EvdiRect)), 0)
        self._lib.evdi_register_buffer(self._handle, buf)
        self._buf_registered = True

    def _grab(self) -> None:
        n = ctypes.c_int(_MAX_RECTS)
        self._lib.evdi_grab_pixels(self._handle, self._rects, ctypes.byref(n))

    def _on_update_ready(self, _buffer_id: int, _user) -> None:
        self._grab()

    def _event_loop(self) -> None:
        fd = self._lib.evdi_get_event_ready(self._handle)
        while not self._stop.is_set():
            if self._buf_registered and self._lib.evdi_request_update(self._handle, _BUFFER_ID):
                self._grab()
            ready, _, _ = select.select([fd], [], [], 1 / 60)
            if ready and not self._stop.is_set():
                self._lib.evdi_handle_events(self._handle, ctypes.byref(self._ctx))

    def stop(self) -> None:
        if self._handle is None:
            return
        try:
            if self.region:
                _xrandr("--output", self.region.output, "--off")
        except Exception:
            pass
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._buf_registered:
            self._lib.evdi_unregister_buffer(self._handle, _BUFFER_ID)
            self._buf_registered = False
        self._lib.evdi_disconnect(self._handle)
        self._lib.evdi_close(self._handle)
        self._handle = None
        self.region = None
        log.info("evdi virtual monitor unplugged")
