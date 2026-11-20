"""Entry point: ``uv run python -m assistant_helper`` (or ./run.sh)."""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from .assistant import Assistant
from .config import load_config
from .llm import MlxLLM
from .server import create_app
from .stt import ParakeetSTT


def main() -> None:
    ap = argparse.ArgumentParser(description="CP Assistant helper")
    ap.add_argument("--no-capture", action="store_true", help="don't open audio devices (typed prompts only)")
    ap.add_argument("--debug", action="store_true",
                    help="enable /debug/utterance and /debug/feed-file (testing without a microphone)")
    ap.add_argument("--log-level", default=os.environ.get("CP_ASSISTANT_LOG", "INFO"))
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "huggingface_hub", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Offline at runtime: models must already be in the HF cache (./setup.sh downloads them).
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    cfg = load_config()
    assistant = Assistant(cfg, ParakeetSTT(cfg["sttModel"]), MlxLLM(cfg["llmModel"], float(cfg.get("wireMaxGB", 10))))
    app = create_app(assistant, load_models=True, capture=not args.no_capture, debug=args.debug)
    uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
