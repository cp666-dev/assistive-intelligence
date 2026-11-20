# UA Assistive Intelligence helper

A local process for the CP Assistant LUNA Add On (`../assistant/`). It listens to the room mics,
transcribes speech, decides whether something was a request, and turns requests into Plans
(lists of LUNA actions) that the panel shows as cards. It runs entirely on this Mac. After setup
nothing leaves the machine: models load from the local Hugging Face cache with `HF_HUB_OFFLINE=1`.

The wire contract with the panel is `../assistant/HELPER-PROTOCOL.md`.

| Part | What |
|---|---|
| HTTP + SSE | Starlette/uvicorn on `127.0.0.1:8791` (`CP_ASSISTANT_PORT`). Every response has CORS `*`, and `OPTIONS` returns 204. |
| Capture | `sounddevice` (PortAudio/CoreAudio). One stream per device; each requested channel (or stereo pair, summed to mono) is resampled to 16 kHz with soxr and handled separately. |
| VAD | Silero VAD v5 (ONNX, bundled in `assistant_helper/data/`). An utterance ends after 700 ms of silence and is capped at 30 s. |
| STT | NVIDIA Parakeet TDT 0.6B v3 via `parakeet-mlx`. While someone speaks, the growing buffer is re-decoded about every 1 s to produce partials; a final follows the end of speech. |
| Vocabulary | `vocabulary.json` corrects studio terms ("dee bee ex one sixty" → "dbx 160", "la two a" → "LA-2A", "over heads" → "overheads"). It is applied to speech and typed prompts. |
| LLM | `mlx-lm`, default `mlx-community/Qwen3-8B-4bit`, about 5 GB resident. Chosen by `smoke/bench.py`: it passed every case the 14B did, faster and at a third of the memory; the 35B-A3B (21 GB) starved LUNA. |
| Memory guard | Every 3 s the helper reads macOS memory pressure. On a warning it unloads the language model (the panel's brain dot turns amber: paused) and reloads it after 60 s of normal pressure. Models over `wireMaxGB` (10) are never wired into RAM. LUNA always comes first. |

## Setup

```sh
cd assistant-helper
./setup.sh          # uv sync from uv.lock (Python 3.12) + downloads both models (~2.5 GB + ~4.6 GB)
./run.sh            # foreground; Ctrl-C to stop
```

`./run.sh --no-capture` starts without opening audio devices (typed prompts only).
`./run.sh --debug` also enables `POST /debug/utterance {text}` (pretend speech, which goes
through the gate) and `POST /debug/feed-file {path}` (sends an audio file through the live
capture path).

### Start at login

```sh
./install-login-item.sh     # writes ~/Library/LaunchAgents/com.cp.assistant.helper.plist (RunAtLoad, KeepAlive) and loads it
./uninstall-login-item.sh   # unloads and removes it
```

Logs go to `~/Library/Logs/CP Assistant/helper.log` and `helper.err.log`.

### Microphone permission

macOS asks for microphone access the first time the helper opens an input device, which happens
on the first `/listen` from the panel. The request is made for the process that runs the capture:

- When started from a terminal, the permission belongs to the terminal app (Terminal, iTerm, …).
- When started as a LaunchAgent, macOS attributes it to the `uv`/`python` binary or shows a
  prompt for it. If no prompt appears and `/listen` opens the device but only silence arrives,
  open System Settings → Privacy & Security → Microphone and enable the entry, then restart the
  helper (`launchctl kickstart -k gui/$(id -u)/com.cp.assistant.helper`).

## Files you can edit

All are in `~/Library/Application Support/CP Assistant/` (set `CP_ASSISTANT_HOME` to use a
different folder). They are created on first run.

**`config.json`**

```jsonc
{
  "port": 8791,
  "device": null,                       // e.g. "Universal Audio Thunderbolt"; null = use the device the panel names
  "channels": { "MIC/LINE 8": 7 },      // per LUNA input name -> 0-based hardware channel (overrides the panel)
  "sttModel": "mlx-community/parakeet-tdt-0.6b-v3",
  "llmModel": "mlx-community/Qwen3-8B-4bit",
  "vad": { "threshold": 0.5, "negThreshold": 0.35, "silenceMs": 700, "maxUtteranceS": 30, "minSpeechMs": 250, "preRollMs": 300 },
  "partialIntervalS": 1.0,
  "historySize": 10,                    // utterances of conversation context
  "pendingPlanTtlS": 300,               // how long an un-actioned plan can be retracted/revised
  "gate": true,
  "maxPlanTokens": 900
}
```

To switch models, change `llmModel`, run `uv run hf download <id>` once while online, then
restart. `mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit` (17 GB) is a smaller fallback.

**`vocabulary.json`** holds the STT corrections and is reloaded whenever it changes.

**`profile.json`** holds saved setups, learned preferences and the feedback log (see below).

## How a request becomes a plan

1. **Gate.** Speech only; typed `/prompt` text skips it. A short LLM call with its own cached
   system prompt (about 0.3–0.7 s) asks whether the last utterance is a request to act on, given
   the previous few utterances and any pending proposals. Praise, chat and musical discussion
   answer NO. Fillers ("yeah", "ok") are dropped before the LLM sees them.
2. **Plan.** The system prompt holds the rules and the panel's catalog (with JSON Schemas). Its KV
   cache is computed once per catalog, when `/catalog` arrives. The user message holds the
   learned preferences, the saved-setup names, a compact SessionSnapshot, the last 10 utterances
   with the plans they produced, pending plans, and the request.
3. **Validation.** Every step is checked against the catalog's JSON Schemas (`jsonschema`). An
   invalid answer gets one repair round with the errors listed. If it is still invalid, only the
   valid steps are kept, with confidence ≤ 0.45. A plan with unknown actions never reaches the
   panel.
4. **Click cuts.** These always come as the cues-only version in `steps` with `choice: "Cues only"`,
   plus an alternative `"Everywhere"` that is a complete step list.
5. **Revisions.** If the model marks a request as revising a pending plan ("actually make it
   stereo"), the helper sends `retract` for the old plan and then the new plan.
6. **Saved setups.** "My usual string quartet setup" is fuzzy-matched (rapidfuzz) against recipe
   names and aliases. The matched recipe goes into the prompt, and the model uses `applyRecipe`
   or adapts the recipe's steps. "Save this as my X setup" becomes `saveRecipe`.

The MLX models run on one worker thread. A plan is ready about 2–5 s after the end of speech
(see "Tests" below).

## How learning works

The panel sends `/feedback` after a plan is run, dismissed or fails, including operator edits
seen within 2 minutes. The helper appends the event to `profile.json → feedback` (last 300) and
updates `preferences` step by step:

| Preference | Learned from |
|---|---|
| `naming` `{"Kick": "Kick In"}` | `rename` edits after Go |
| `plugins` `{"comp bus": "dbx 160"}` | plugins inserted by plans that ran (role = track/bus name without numbers), and `plugin` edits |
| `formats` `{"overheads": "stereo"}` | mono/stereo of tracks created by plans that ran |
| `avoid` `["setRecordArm"]` | a kind of plan (its set of actions) dismissed at least twice and more than twice as often as accepted; it is removed again once accepted |
| `notes` `["Always call the vocal Lead Vox"]` | only you; the helper never writes here |

Every preference goes into the planning prompt as plain text, so you can check what the model
sees with `GET /profile`. You can edit `profile.json` while the helper runs; it reloads on
change, and learning never overwrites your edits wholesale.

## Tests

```sh
uv run pytest                          # HTTP contract, SSE, flow, vocab, recipes, validation, learning, VAD (mocked LLM/STT)
CP_ASSISTANT_REAL=1 uv run pytest tests/test_pipeline.py   # + real Parakeet: `say` → 48 kHz → soxr → VAD → STT
./run.sh --no-capture --debug &  uv run python smoke/smoke.py   # real LLM end to end (use a scratch CP_ASSISTANT_HOME)
```

The smoke test posts the panel's real catalog (`tests/fixtures/catalog.json`, taken from
`assistant/logic.js`) and three session snapshots. It runs the three reference prompts, five
chatter lines and three indirect requests through the gate, and one spoken sentence through
VAD + Parakeet. Results go to `smoke/out/results.json`.
