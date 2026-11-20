"""Live capture from a CoreAudio device via sounddevice (PortAudio).

One InputStream per device, opened with enough channels to reach the highest requested channel.
Each requested input (one hardware channel, or a pair for stereo, mixed to mono) is resampled to
16 kHz and handed to ``on_audio(input_name, samples)`` on that input's own worker thread, so the
control-room mic and the live-room mic are segmented independently.

A watchdog reopens streams whose device vanished / stopped delivering audio and reports through
``on_status``.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from . import coreaudio

log = logging.getLogger(__name__)
TARGET_SR = 16000
# A tap that has delivered nothing but digital zeros for this long is reported: macOS gives a
# process without microphone permission silence instead of an error.
SILENT_AFTER_S = 4.0


@dataclass
class ListenInput:
    name: str
    device: str
    channel: int | None  # from config.json; else resolved when the device opens
    format: str = "mono"
    guess: int | None = None  # the panel's channel from the input's number


    @property
    def channels(self) -> list[int]:
        return [self.channel, self.channel + 1] if self.format == "stereo" else [self.channel]


@dataclass
class _InputTap:
    spec: ListenInput
    resampler: object
    q: "queue.Queue[np.ndarray | None]" = field(default_factory=lambda: queue.Queue(maxsize=400))
    thread: threading.Thread | None = None
    peak: float = 0.0  # highest |sample| since the levels task last read it (linear, 0..1)
    heard: bool = False  # any non-zero sample since the stream opened


class _DeviceStream:
    def __init__(self, device_index: int, device_name: str, samplerate: float, n_channels: int,
                 taps: list[_InputTap]):
        import sounddevice as sd

        self.device_name = device_name
        self.samplerate = samplerate
        self.taps = taps
        self.last_callback = time.monotonic()
        self.opened_at = time.monotonic()
        self.overflows = 0
        self.stream = sd.InputStream(
            device=device_index, channels=n_channels, samplerate=samplerate, dtype="float32",
            blocksize=int(samplerate * 0.032), latency="low", callback=self._callback,
        )
        self.stream.start()

    def _callback(self, indata, frames, time_info, status):  # PortAudio thread: keep it cheap
        self.last_callback = time.monotonic()
        if status and status.input_overflow:
            self.overflows += 1
        for tap in self.taps:
            chans = tap.spec.channels
            for c in chans:  # block peak per channel; reductions on views, nothing allocated
                p = max(float(indata[:, c].max()), -float(indata[:, c].min()))
                if p > tap.peak:
                    tap.peak = p
                if p > 0.0:
                    tap.heard = True
            x = indata[:, chans[0]] if len(chans) == 1 else indata[:, chans].mean(axis=1)
            try:
                tap.q.put_nowait(np.array(x, dtype=np.float32))
            except queue.Full:
                pass  # consumer is stuck; drop rather than block the audio thread

    @property
    def healthy(self) -> bool:
        return self.stream.active and time.monotonic() - self.last_callback < 2.5

    def close(self) -> None:
        try:
            self.stream.abort()
            self.stream.close()
        except Exception:
            pass


class CaptureManager:
    def __init__(self, on_audio: Callable[[str, np.ndarray], None], on_flush: Callable[[str], None],
                 on_status: Callable[[], None], device_override: str | None = None,
                 channel_override: Callable[[str], int | None] = lambda name: None):
        self.on_audio = on_audio
        self.on_flush = on_flush
        self.on_status = on_status
        self.device_override = device_override
        self.channel_override = channel_override
        self._lock = threading.RLock()
        self._wanted: list[ListenInput] = []
        self.enabled = False
        self._streams: dict[str, _DeviceStream] = {}
        self._taps: dict[str, _InputTap] = {}
        self._open_errors: list[str] = []
        self._silent: list[str] = []
        self._stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch, name="audio-watchdog", daemon=True)
        self._watchdog.start()

    # ---- public ------------------------------------------------------------------------
    @property
    def errors(self) -> list[str]:
        return self._open_errors + self._silent

    @property
    def listening(self) -> bool:
        return self.enabled and bool(self._streams)

    @property
    def open_inputs(self) -> list[str]:
        with self._lock:
            return [name for s in self._streams.values() for name in (t.spec.name for t in s.taps)]

    def take_peaks(self) -> dict[str, float]:
        """Peak |sample| per open input since the last call, then reset (for the levels event)."""
        with self._lock:
            out = {}
            for s in self._streams.values():
                for tap in s.taps:
                    out[tap.spec.name] = tap.peak
                    tap.peak = 0.0
            return out

    def configure(self, inputs: list[dict], enabled: bool) -> tuple[list[str], list[str]]:
        with self._lock:
            self._close_all()
            self.enabled = bool(enabled)
            self._wanted = []
            self._silent = []
            for raw in inputs or []:
                name = str(raw.get("name") or f"input {raw.get('channel')}")
                guess = raw.get("channel")
                self._wanted.append(ListenInput(
                    name=name, device=self.device_override or str(raw.get("device") or ""),
                    channel=self.channel_override(name), format="stereo" if raw.get("format") == "stereo" else "mono",
                    guess=int(guess) if guess is not None else None,
                ))
            if self.enabled:
                self._open_wanted(rescan=True)
            else:
                self._open_errors = []
            opened, errors = self.open_inputs, list(self.errors)
        self.on_status()
        return opened, errors

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            self._close_all()

    # ---- internals ---------------------------------------------------------------------
    def _close_all(self) -> None:
        for s in self._streams.values():
            s.close()
        self._streams = {}
        for tap in self._taps.values():
            tap.q.put(None)  # tells the worker to flush and exit
        self._taps = {}

    @staticmethod
    def _rescan() -> None:
        """PortAudio caches the device list; re-init to see plugged / unplugged devices."""
        import sounddevice as sd

        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:
            log.warning("PortAudio re-init failed: %s", e)

    @staticmethod
    def _find_device(name: str) -> tuple[int, dict] | None:
        import sounddevice as sd

        want = (name or "").lower()
        devices = list(enumerate(sd.query_devices()))
        candidates = [(i, d) for i, d in devices if d["max_input_channels"] > 0]
        # LUNA's deviceId is the CoreAudio UID ("BuiltInMicrophoneDevice"); PortAudio lists names.
        by_uid = next((d for d in coreaudio.input_devices() if d.uid and d.uid.lower() == want), None)
        if by_uid:
            for i, d in candidates:
                if d["name"] == by_uid.name:
                    return i, d
        if not want or "uaudio" in want or "uad2" in want:
            # LUNA leaves deviceId empty for its interface inputs, or (for talkback) gives the
            # UA driver's id "com_uaudio_driver_UAD2AudioEngine:0": prefer the UA interface,
            # whose channels the panel's input numbers refer to, over the Mac's default input.
            for i, d in candidates:
                if "universal audio" in d["name"].lower() or "apollo" in d["name"].lower():
                    return i, d
            idx = sd.default.device[0]
            return (idx, sd.query_devices(idx)) if idx is not None and idx >= 0 else None
        for i, d in candidates:
            if d["name"].lower() == want:
                return i, d
        for i, d in candidates:
            if want in d["name"].lower() or d["name"].lower() in want:
                return i, d
        return None

    @staticmethod
    def _resolve_channels(specs: list[ListenInput], device_name: str,
                          device_inputs: int | None = None) -> tuple[list[ListenInput], list[str]]:
        """Each input's hardware channel: config.json, else the driver's name for the channel
        (exact), else the panel's guess from the input's number, else channel 0 when the device
        has no more channels than the input uses (a laptop's one mic: nothing to choose)."""
        from dataclasses import replace

        names = next((d.channel_names for d in coreaudio.input_devices() if d.name == device_name), [])
        out, errors = [], []
        for s in specs:
            ch = s.channel
            if ch is None:
                ch = coreaudio.channel_for(s.name, names)
            if ch is None:
                ch = s.guess
            if ch is None and device_inputs is not None and device_inputs <= (2 if s.format == "stereo" else 1):
                ch = 0
            if ch is None:
                # Guessing channel 0 would listen to (and meter) another input under this name.
                errors.append(f'{s.name}: no hardware channel known; set "channels": {{"{s.name}": <0-based channel>}} in config.json')
                continue
            out.append(replace(s, channel=ch))
        return out, errors

    def _open_wanted(self, rescan: bool) -> None:
        if rescan and not self._streams:
            self._rescan()
        self._open_errors = []
        by_device: dict[str, list[ListenInput]] = {}
        for spec in self._wanted:
            by_device.setdefault(spec.device, []).append(spec)
        for dev_name, specs in by_device.items():
            if dev_name in self._streams:
                continue
            found = self._find_device(dev_name)
            if not found:
                for s in specs:
                    self._open_errors.append(f"{s.name}: audio device '{dev_name}' not found")
                continue
            idx, info = found
            specs, unmapped = self._resolve_channels(specs, info["name"], int(info["max_input_channels"]))
            self._open_errors.extend(unmapped)
            max_in = int(info["max_input_channels"])
            usable = []
            for s in specs:
                if max(s.channels) >= max_in:
                    self._open_errors.append(
                        f"{s.name}: channel {max(s.channels)} out of range ('{info['name']}' has {max_in} inputs)")
                else:
                    usable.append(s)
            if not usable:
                continue
            n_ch = max(max(s.channels) for s in usable) + 1
            sr = float(info["default_samplerate"])
            taps = []
            for s in usable:
                import soxr

                tap = _InputTap(spec=s, resampler=soxr.ResampleStream(sr, TARGET_SR, 1, dtype="float32"))
                tap.thread = threading.Thread(target=self._consume, args=(tap,), name=f"in:{s.name}", daemon=True)
                taps.append(tap)
            try:
                stream = _DeviceStream(idx, info["name"], sr, n_ch, taps)
            except Exception as e:
                for s in usable:
                    self._open_errors.append(f"{s.name}: cannot open '{info['name']}': {e}")
                continue
            self._streams[dev_name] = stream
            for tap in taps:
                self._taps[tap.spec.name] = tap
                tap.thread.start()
            log.info("capturing %s from '%s' @ %.0f Hz (%d ch)",
                     [t.spec.name for t in taps], info["name"], sr, n_ch)

    def _consume(self, tap: _InputTap) -> None:
        while True:
            x = tap.q.get()
            if x is None:
                self.on_flush(tap.spec.name)
                return
            y = tap.resampler.resample_chunk(x)
            if len(y):
                try:
                    self.on_audio(tap.spec.name, y)
                except Exception:
                    log.exception("audio pipeline error on %s", tap.spec.name)

    def _watch(self) -> None:
        last_sig = None
        while not self._stop.wait(2.0):
            with self._lock:
                if not self.enabled or not self._wanted:
                    continue
                dead = [k for k, s in self._streams.items() if not s.healthy]
                for k in dead:
                    log.warning("audio device '%s' stopped delivering; reopening", k)
                    s = self._streams.pop(k)
                    for tap in s.taps:
                        self._taps.pop(tap.spec.name, None)
                        tap.q.put(None)
                    s.close()
                missing = {spec.device for spec in self._wanted} - set(self._streams)
                if missing:
                    self._open_wanted(rescan=True)
                now = time.monotonic()
                self._silent = [
                    f"{t.spec.name}: no sound at all from '{s.device_name}'. Turn the input on, or allow the "
                    "assistant in System Settings > Privacy & Security > Microphone"
                    for s in self._streams.values() if now - s.opened_at > SILENT_AFTER_S
                    for t in s.taps if not t.heard
                ]
                sig = (tuple(self.open_inputs), tuple(self.errors))
            if sig != last_sig:
                last_sig = sig
                self.on_status()


class LevelMeter:
    """Turns raw block peaks into the panel's ``levels`` event: dBFS clamped to -60..0, peak hold
    with a 20 dB/s fall, ``None`` at the floor (silent) or for a closed input. ``update`` returns
    the levels to send, or ``None`` when nothing moved by 1 dB or more since the last send."""

    FLOOR_DB = -60.0
    DECAY_DB_PER_S = 20.0
    STEP_DB = 1.0

    def __init__(self) -> None:
        self._shown: dict[str, float] = {}
        self._sent: dict[str, float | None] | None = None

    @classmethod
    def to_db(cls, peak: float) -> float:
        if not peak > 0:
            return cls.FLOOR_DB
        return max(cls.FLOOR_DB, min(0.0, 20.0 * math.log10(peak)))

    def update(self, peaks: dict[str, float], dt: float) -> dict[str, float | None] | None:
        shown = {}
        for name, peak in peaks.items():
            fallen = self._shown.get(name, self.FLOOR_DB) - self.DECAY_DB_PER_S * max(0.0, dt)
            shown[name] = max(self.to_db(peak), fallen, self.FLOOR_DB)
        self._shown = shown
        levels = {name: None if v <= self.FLOOR_DB else round(v, 1) for name, v in shown.items()}
        if not self._changed(levels):
            return None
        self._sent = levels
        return levels

    def silence(self) -> dict[str, None] | None:
        """Listening stopped: one event with every input null, then nothing until levels return."""
        self._shown = {}
        if not self._sent:
            self._sent = None
            return None
        levels = {name: None for name in self._sent}
        self._sent = None
        return levels

    def _changed(self, levels: dict[str, float | None]) -> bool:
        sent = self._sent
        if sent is None or sent.keys() != levels.keys():
            return True
        for name, v in levels.items():
            w = sent[name]
            if (v is None) != (w is None):
                return True
            if v is not None and abs(v - w) >= self.STEP_DB:
                return True
        return False
