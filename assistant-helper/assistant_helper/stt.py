"""Speech-to-text with NVIDIA Parakeet TDT 0.6B v3 via parakeet-mlx (runs on the MLX thread)."""

from __future__ import annotations

import logging
import time

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class BaseSTT:
    status = "loading"

    def load(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def transcribe(self, audio: np.ndarray) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class ParakeetSTT(BaseSTT):
    def __init__(self, model_id: str):
        self.model_id = model_id
        self.model = None
        self.status = "loading"

    def load(self) -> None:
        from parakeet_mlx import from_pretrained

        t0 = time.time()
        try:
            self.model = from_pretrained(self.model_id)
            # warm-up: compiles kernels so the first real utterance is fast
            self.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))
        except Exception:
            self.status = "error"
            raise
        assert self.model.preprocessor_config.sample_rate == SAMPLE_RATE
        log.info("STT %s ready in %.1fs", self.model_id, time.time() - t0)
        self.status = "ready"

    def transcribe(self, audio: np.ndarray) -> str:
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        if audio.size < 1600:  # < 0.1 s
            return ""
        x = mx.array(audio.astype(np.float32))
        # get_logmel is wrapped in an unbounded lru_cache keyed on the array object; calling
        # it per utterance would keep every utterance alive forever, so bypass the cache.
        logmel = getattr(get_logmel, "__wrapped__", get_logmel)
        mel = logmel(x, self.model.preprocessor_config)
        result = self.model.generate(mel)[0]
        return result.text.strip()
