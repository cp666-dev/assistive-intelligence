"""What CoreAudio knows about input devices, read directly through ctypes (no extra packages).

LUNA names a track's input device by its CoreAudio UID (``BuiltInMicrophoneDevice``,
``com_uaudio_driver_UAD2AudioEngine:0``), while PortAudio lists devices by display name, so the
UID is the reliable way to find the device. Many interface drivers also name their channels
("MIC/LINE 1", "TALKBACK 1"): the same names LUNA shows, which gives the hardware channel of an
input without guessing from its number.

Everything here is best effort: on any failure it returns nothing and callers fall back.
"""

from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_SYSTEM_OBJECT = 1
_MAIN_ELEMENT = 0
_UTF8 = 0x08000100


def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode("ascii"), "big")


_DEVICES = _fourcc("dev#")
_GLOBAL = _fourcc("glob")
_INPUT = _fourcc("inpt")
_NAME = _fourcc("lnam")
_UID = _fourcc("uid ")
_STREAM_CONFIG = _fourcc("slay")
_ELEMENT_NAME = _fourcc("lchn")


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


@dataclass
class Device:
    uid: str
    name: str
    inputs: int
    channel_names: list[str] = field(default_factory=list)  # one per input channel, "" when unnamed


_lib: tuple | None = None


def _libs():
    global _lib
    if _lib is None:
        ca = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
                                                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
                                                 ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        _lib = (ca, cf)
    return _lib


def _data(obj: int, selector: int, scope: int, element: int = _MAIN_ELEMENT) -> bytes | None:
    ca, _ = _libs()
    addr = _Address(selector, scope, element)
    size = ctypes.c_uint32(0)
    if ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)) != 0 or not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf) != 0:
        return None
    return buf.raw[: size.value]


def _string(obj: int, selector: int, scope: int, element: int = _MAIN_ELEMENT) -> str:
    ca, cf = _libs()
    addr = _Address(selector, scope, element)
    ref = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(ref))
    if ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(ref)) != 0 \
            or not ref.value:
        return ""
    try:
        out = ctypes.create_string_buffer(1024)
        return out.value.decode("utf-8") if cf.CFStringGetCString(ref, out, 1024, _UTF8) else ""
    finally:
        cf.CFRelease(ref)


def _input_channels(obj: int) -> int:
    raw = _data(obj, _STREAM_CONFIG, _INPUT)
    if not raw or len(raw) < 4:
        return 0
    n_buffers = int.from_bytes(raw[0:4], "little")
    # AudioBufferList: UInt32 count, padding to 8, then AudioBuffer {UInt32 channels; UInt32 size; void *data}.
    return sum(int.from_bytes(raw[8 + 16 * i: 12 + 16 * i], "little") for i in range(n_buffers)
               if 12 + 16 * i <= len(raw))


def input_devices() -> list[Device]:
    """Every device with inputs, or [] when CoreAudio can't be read."""
    try:
        raw = _data(_SYSTEM_OBJECT, _DEVICES, _GLOBAL) or b""
        ids = [int.from_bytes(raw[i:i + 4], "little") for i in range(0, len(raw), 4)]
        out = []
        for dev in ids:
            n = _input_channels(dev)
            if n <= 0:
                continue
            names = [_string(dev, _ELEMENT_NAME, _INPUT, ch) for ch in range(1, n + 1)]
            out.append(Device(uid=_string(dev, _UID, _GLOBAL), name=_string(dev, _NAME, _GLOBAL), inputs=n,
                              channel_names=names))
        return out
    except Exception as e:  # never let device discovery stop capture
        log.warning("CoreAudio device list unavailable: %s", e)
        return []


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("-", " - ").split())


def channel_for(input_name: str, channel_names: list[str]) -> int | None:
    """The 0-based channel whose driver name is LUNA's input name. A stereo pair ("TALKBACK 1-2",
    "MIC/LINE 3/4") starts on its first channel ("TALKBACK 1", "MIC/LINE 3")."""
    if not input_name or not any(channel_names):
        return None
    names = [_norm(n) for n in channel_names]
    want = _norm(input_name)
    if want in names:
        return names.index(want)
    import re

    pair = re.match(r"^(.*?)(\d+)\s*[-/]\s*\d+$", input_name.strip())
    if pair:
        first = _norm(f"{pair[1]}{pair[2]}")
        if first in names:
            return names.index(first)
    return None
