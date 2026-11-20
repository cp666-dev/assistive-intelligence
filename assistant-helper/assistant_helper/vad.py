"""Silero VAD (v5, ONNX on CPU) and an utterance segmenter driven by it.

Timing is counted in audio samples, not wall-clock, so a file fed through the segmenter behaves
exactly like live audio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

SR = 16000
FRAME = 512  # Silero v5 frame at 16 kHz (32 ms)
CONTEXT = 64
MODEL_PATH = Path(__file__).parent / "data" / "silero_vad.onnx"


class SileroVAD:
    """One instance per audio stream (it is stateful)."""

    _session = None  # shared ONNX session; state is kept per instance

    def __init__(self, model_path: Path = MODEL_PATH):
        import onnxruntime as ort

        if SileroVAD._session is None:
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            SileroVAD._session = ort.InferenceSession(str(model_path), sess_options=opts,
                                                      providers=["CPUExecutionProvider"])
        self.session = SileroVAD._session
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)
        self._sr = np.array(SR, dtype=np.int64)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._context, frame.reshape(1, FRAME).astype(np.float32)], axis=1)
        out, self._state = self.session.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._context = x[:, -CONTEXT:]
        return float(out[0][0])


@dataclass
class VadParams:
    threshold: float = 0.5
    negThreshold: float = 0.35
    silenceMs: int = 700
    maxUtteranceS: float = 30.0
    minSpeechMs: int = 250
    preRollMs: int = 300
    partialIntervalS: float = 1.0

    @classmethod
    def from_config(cls, vad: dict, partial_interval: float) -> "VadParams":
        known = {k: v for k, v in (vad or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known, partialIntervalS=partial_interval)


@dataclass
class Segmenter:
    """Feed 16 kHz mono float32; get callbacks for utterance start / partial / end."""

    vad: Callable[[np.ndarray], float]
    params: VadParams
    new_id: Callable[[], str]
    on_start: Callable[[str], None] = lambda uid: None
    on_partial: Callable[[str, np.ndarray], None] = lambda uid, audio: None
    on_end: Callable[[str, np.ndarray], None] = lambda uid, audio: None

    _pending: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    _preroll: list = field(default_factory=list)
    _speech: list = field(default_factory=list)
    _uid: str | None = None
    _silence: int = 0
    _voiced: int = 0
    _total: int = 0
    _since_partial: int = 0

    @property
    def in_speech(self) -> bool:
        return self._uid is not None

    def feed(self, samples: np.ndarray) -> None:
        buf = np.concatenate([self._pending, samples.astype(np.float32, copy=False)])
        n = len(buf) // FRAME
        for i in range(n):
            self._frame(buf[i * FRAME : (i + 1) * FRAME])
        self._pending = buf[n * FRAME :]

    def flush(self) -> None:
        """End any utterance in progress (e.g. capture stopped / end of file)."""
        if self._uid is not None:
            self._finish()

    def _frame(self, frame: np.ndarray) -> None:
        p = self.params
        prob = self.vad(frame)
        if self._uid is None:
            self._preroll.append(frame)
            max_pre = max(1, int(p.preRollMs * SR / 1000 / FRAME))
            del self._preroll[:-max_pre]
            if prob >= p.threshold:
                self._uid = self.new_id()
                self._speech = list(self._preroll)
                self._preroll = []
                self._silence = 0
                self._voiced = FRAME
                self._total = sum(len(f) for f in self._speech)
                self._since_partial = 0
                self.on_start(self._uid)
            return

        self._speech.append(frame)
        self._total += FRAME
        self._since_partial += FRAME
        if prob >= p.threshold:
            self._voiced += FRAME
            self._silence = 0
        elif prob < p.negThreshold:
            self._silence += FRAME
        else:
            self._silence = 0 if self._silence == 0 else self._silence + FRAME

        if self._silence >= p.silenceMs * SR / 1000 or self._total >= p.maxUtteranceS * SR:
            self._finish()
        elif self._since_partial >= p.partialIntervalS * SR:
            self._since_partial = 0
            self.on_partial(self._uid, np.concatenate(self._speech))

    def _finish(self) -> None:
        uid, frames, voiced, silence = self._uid, self._speech, self._voiced, self._silence
        self._uid, self._speech, self._silence, self._voiced = None, [], 0, 0
        self._preroll = []
        if not frames:
            return
        audio = np.concatenate(frames)
        # trim the trailing silence that ended the utterance, keep ~200 ms of it
        trail = max(0, silence - int(0.2 * SR))
        if 0 < trail < len(audio):
            audio = audio[: len(audio) - trail]
        if voiced < self.params.minSpeechMs * SR / 1000:
            self.on_end(uid, np.zeros(0, dtype=np.float32))  # too short: a blip, not speech
            return
        self.on_end(uid, audio)
