"""Audio file -> (device-rate) chunks -> soxr -> Silero VAD segmenter -> STT, the live path.

Uses macOS `say` to synthesise speech. With CP_ASSISTANT_REAL=1 the real Parakeet model is
used (must be downloaded); otherwise a fake STT checks the segmentation only.
"""

import os
import shutil
import subprocess
import time

import numpy as np
import pytest
import soxr

from assistant_helper.config import load_config
from assistant_helper.events import EventBus
from assistant_helper.assistant import Assistant

from conftest import FakeLLM, FakeSTT

pytestmark = pytest.mark.skipif(not shutil.which("say") or not shutil.which("afconvert"), reason="macOS only")
REAL = os.environ.get("CP_ASSISTANT_REAL") == "1"


def say_to_48k(text: str, tmp_path) -> np.ndarray:
    aiff, wav = tmp_path / "x.aiff", tmp_path / "x.wav"
    subprocess.run(["say", "-o", str(aiff), text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEF32@48000", "-c", "1", str(aiff), str(wav)], check=True)
    import soundfile as sf

    data, sr = sf.read(str(wav), dtype="float32")
    assert sr == 48000
    return data


def feed_like_live(a: Assistant, name: str, audio48: np.ndarray, block=1536):
    rs = soxr.ResampleStream(48000, 16000, 1, dtype="float32")  # same as audio.CaptureManager
    silence = np.zeros(48000 * 2, dtype=np.float32)  # 2 s of room tone after speech
    x = np.concatenate([np.zeros(24000, dtype=np.float32), audio48, silence])
    for i in range(0, len(x), block):
        y = rs.resample_chunk(x[i : i + block])
        if len(y):
            a.feed_audio(name, y)


def make_assistant(home, stt):
    cfg = load_config()
    a = Assistant(cfg, stt, FakeLLM(), EventBus())
    return a


@pytest.fixture(scope="module")
def real_stt():
    if not REAL:
        pytest.skip("set CP_ASSISTANT_REAL=1 to run Parakeet")
    from assistant_helper.stt import ParakeetSTT

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    # loaded later on the assistant's MLX thread (MLX streams are per thread)
    return ParakeetSTT("mlx-community/parakeet-tdt-0.6b-v3")


def run_file(a: Assistant, audio48):
    feed_like_live(a, "MIC/LINE 8", audio48)
    a.mlx.submit(lambda: None).result(timeout=60)  # drain STT jobs
    a.mlx.submit(lambda: None).result(timeout=60)
    return [e for e in a.events.history if e["type"] == "transcript"]


def test_vad_segments_speech_fake_stt(home, tmp_path):
    stt = FakeSTT("ok")
    a = make_assistant(home, stt)
    audio = say_to_48k("Create a new send off of kick and snare to a compression bus.", tmp_path)
    ts = run_file(a, audio)
    finals = [t for t in ts if t["final"]]
    assert len(finals) == 1 and finals[0]["input"] == "MIC/LINE 8"
    assert stt.calls >= 2  # at least one partial + the final


def test_vad_ignores_noise(home):
    stt = FakeSTT("ok")
    a = make_assistant(home, stt)
    rng = np.random.default_rng(0)
    ts = run_file(a, (rng.standard_normal(48000 * 4) * 0.003).astype(np.float32))
    assert ts == [] and stt.calls == 0


def test_parakeet_end_to_end(home, tmp_path, real_stt):
    a = make_assistant(home, real_stt)
    a.mlx.submit(real_stt.load).result(timeout=120)
    audio = say_to_48k("Put a dee bee ex one sixty on the compression bus, and an L.A. two A on the vocal.", tmp_path)
    t0 = time.time()
    ts = run_file(a, audio)
    finals = [t for t in ts if t["final"]]
    partials = [t for t in ts if not t["final"]]
    print("\npartials:", [p["text"] for p in partials])
    print("final:", finals[0]["text"], f"({time.time() - t0:.2f}s for {len(audio) / 48000:.1f}s audio)")
    assert len(finals) == 1
    text = finals[0]["text"].lower()
    assert "compression bus" in text and "vocal" in text
    assert "dbx 160" in text
    assert partials, "expected at least one partial transcript"
