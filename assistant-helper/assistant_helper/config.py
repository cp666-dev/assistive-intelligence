"""Paths and user configuration.

Everything user-editable lives in ``~/Library/Application Support/CP Assistant/``
(override with ``CP_ASSISTANT_HOME``, used by the tests):

- ``config.json``      device, channel overrides, model ids, VAD tuning
- ``profile.json``     recipes, learned preferences, feedback log
- ``vocabulary.json``  STT domain corrections (copied from the packaged default on first run)
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PACKAGE_DATA = Path(__file__).parent / "data"

DEFAULT_CONFIG: dict[str, Any] = {
    # HTTP port; CP_ASSISTANT_PORT wins over this.
    "port": 8791,
    # CoreAudio device name (substring match). null = use the device named in /listen.
    "device": None,
    # Per LUNA input name -> 0-based hardware channel, overriding the panel's guess.
    # e.g. {"MIC/LINE 1": 0, "MIC/LINE 2": 1}
    "channels": {},
    "sttModel": "mlx-community/parakeet-tdt-0.6b-v3",
    "llmModel": "mlx-community/Qwen3-8B-4bit",
    "vad": {
        "threshold": 0.5,  # speech probability to start / continue speech
        "negThreshold": 0.35,  # below this counts as silence
        "silenceMs": 700,  # end utterance after this much silence
        "maxUtteranceS": 30,  # hard cap
        "minSpeechMs": 250,  # drop blips shorter than this
        "preRollMs": 300,  # audio kept before speech onset
    },
    "partialIntervalS": 1.0,  # re-decode the growing buffer this often while speaking
    "historySize": 10,  # utterances of conversation context
    "pendingPlanTtlS": 300,  # plans without feedback count as "pending" (retractable) this long
    "gate": True,  # false = every final utterance is planned (debug)
    "maxPlanTokens": 900,
}


def home_dir() -> Path:
    env = os.environ.get("CP_ASSISTANT_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "CP Assistant"


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    home: Path
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULT_CONFIG))

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @property
    def profile_path(self) -> Path:
        return self.home / "profile.json"

    @property
    def vocabulary_path(self) -> Path:
        return self.home / "vocabulary.json"

    @property
    def port(self) -> int:
        env = os.environ.get("CP_ASSISTANT_PORT")
        return int(env) if env else int(self.data["port"])

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def channel_override(self, input_name: str) -> int | None:
        chans = {k.lower(): int(v) for k, v in (self.data.get("channels") or {}).items()}
        name = (input_name or "").lower()
        if name in chans:
            return chans[name]
        # A stereo pair ("TALKBACK 1-2") starts on its first channel's mapping ("TALKBACK 1").
        pair = re.match(r"^(.*?)(\d+)\s*[-/]\s*\d+$", name)
        if pair:
            return chans.get(f"{pair[1]}{pair[2]}")
        return None


def load_config(home: Path | None = None) -> Config:
    home = home or home_dir()
    home.mkdir(parents=True, exist_ok=True)
    cfg = Config(home=home)
    if cfg.config_path.exists():
        try:
            user = json.loads(cfg.config_path.read_text())
            cfg.data = _deep_merge(DEFAULT_CONFIG, user)
        except Exception as e:  # keep running on a bad edit, but say so
            log.error("config.json is not valid JSON (%s); using defaults", e)
    else:
        cfg.config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    if not cfg.vocabulary_path.exists():
        shutil.copy(PACKAGE_DATA / "vocabulary.json", cfg.vocabulary_path)
    return cfg
