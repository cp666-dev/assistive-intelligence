#!/bin/bash
# One-time setup: Python env from the lockfile + model downloads (the only network use).
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
uv sync --frozen
STT=$(uv run --frozen python -c 'from assistant_helper.config import load_config; print(load_config()["sttModel"])')
LLM=$(uv run --frozen python -c 'from assistant_helper.config import load_config; print(load_config()["llmModel"])')
echo "Downloading STT model $STT"
uv run --frozen hf download "$STT"
echo "Downloading LLM $LLM (about 5 GB)"
uv run --frozen hf download "$LLM"
echo "Done. Start with ./run.sh"
