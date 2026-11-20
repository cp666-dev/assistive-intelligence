#!/bin/bash
# Start the CP Assistant helper (foreground). Used directly and by the LaunchAgent.
set -euo pipefail
cd "$(dirname "$0")"
# LaunchAgents get a minimal PATH; uv and ffmpeg live in Homebrew.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
# The login item owns the port: a helper left running from another copy (an old checkout or
# build) would keep it and the panel would talk to stale code. Stop it before starting.
# Match only the helper's Python process (uv exits with it), never a shell that mentions it.
HELPER_PY='/python[0-9.]* -m assistant_helper( --[a-z-]+)*$'
if [ "${XPC_SERVICE_NAME:-}" = "com.cp.assistant.helper" ]; then
  others=$(pgrep -f "$HELPER_PY" || true)
  if [ -n "$others" ]; then
    echo "run.sh: stopping other helper(s): $others" >&2
    kill $others 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "$HELPER_PY" >/dev/null || break; sleep 0.5; done
    pkill -9 -f "$HELPER_PY" 2>/dev/null || true
  fi
fi
exec uv run --frozen python -m assistant_helper "$@"
