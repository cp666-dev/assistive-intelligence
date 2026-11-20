"""The `levels` event: per-input peak dBFS for the panel's meters (HELPER-PROTOCOL.md, Events)."""
from __future__ import annotations

import asyncio
import queue

import numpy as np
import pytest

from assistant_helper.assistant import Assistant
from assistant_helper.audio import LevelMeter, ListenInput, _DeviceStream, _InputTap
from assistant_helper.config import load_config

from conftest import FakeLLM, FakeSTT


class FakeCapture:
    def __init__(self):
        self.listening = True
        self.peaks: dict[str, float] = {}

    def take_peaks(self):
        out, self.peaks = self.peaks, {k: 0.0 for k in self.peaks}
        return out

    def shutdown(self):
        pass


def test_levels_event_shape_and_all_null_when_listening_stops(home):
    async def run():
        a = Assistant(load_config(), FakeSTT(), FakeLLM())
        cap = FakeCapture()
        cap.peaks = {"MIC/LINE 1": 10 ** (-23.5 / 20), "TALKBACK 1": 0.0}
        a.capture = cap
        await a.start(load_models=False)
        q = a.events.subscribe()
        try:
            first = await asyncio.wait_for(q.get(), 2)
            cap.listening = False
            last = await asyncio.wait_for(q.get(), 2)
            await asyncio.sleep(0.35)  # and nothing more while not listening
            assert q.empty()
        finally:
            await a.stop()
        return first, last

    first, last = asyncio.run(run())
    assert first == {"type": "levels", "levels": {"MIC/LINE 1": -23.5, "TALKBACK 1": None}}
    assert last == {"type": "levels", "levels": {"MIC/LINE 1": None, "TALKBACK 1": None}}


def test_meter_clamps_holds_decays_and_sends_only_on_1db_moves():
    m = LevelMeter()
    assert m.update({"A": 1.0, "B": 10 ** (-80 / 20)}, 0.1) == {"A": 0.0, "B": None}  # clamp to -60..0
    assert m.update({"A": 0.0, "B": 0.0}, 0.1) == {"A": -2.0, "B": None}  # falls at 20 dB/s
    assert m.update({"A": 10 ** (-2.5 / 20), "B": 0.0}, 0.01) is None  # held -2.0 falls to -2.2, the -2.5 peak is under it: < 1 dB moved
    assert m.update({"A": 0.0, "B": 0.0}, 3.0) == {"A": None, "B": None}  # fell to the floor
    assert m.update({"A": 0.0, "B": 0.0}, 0.1) is None
    assert m.update({"A": 0.0}, 0.1) == {"A": None}  # an input closed
    assert m.silence() == {"A": None}
    assert m.silence() is None


def test_callback_records_block_peak_per_input_without_touching_audio():
    stream = _DeviceStream.__new__(_DeviceStream)
    stream.overflows = 0
    mono = _InputTap(spec=ListenInput("MIC/LINE 1", "dev", 0), resampler=None, q=queue.Queue())
    pair = _InputTap(spec=ListenInput("LINE 2/3", "dev", 2, "stereo"), resampler=None, q=queue.Queue())
    stream.taps = [mono, pair]
    block = np.zeros((512, 4), dtype=np.float32)
    block[10, 0] = -0.5
    block[20, 3] = 0.25
    stream._callback(block, 512, None, None)
    block[:] = 0
    block[5, 0] = 0.1
    stream._callback(block, 512, None, None)
    assert mono.peak == pytest.approx(0.5)  # held until read
    assert pair.peak == pytest.approx(0.25)
    assert mono.q.qsize() == 2  # the audio still goes to the worker
