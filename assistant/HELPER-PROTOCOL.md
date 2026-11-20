# CP Assistant: panel ↔ helper protocol

The **panel** is the LUNA Add On (`assistant/`). It owns everything that touches LUNA: it reads
the session, executes actions and schedules click cuts. The **helper** (`assistant-helper/`) is
a local Python process. It owns audio capture, speech-to-text, the local language model and the
user's long-term profile. Neither talks to anything off the machine.

Verified inside LUNA 3.1.1 (page origin `ua-content://com.cp.assistant`): `fetch` and
`EventSource` to `http://127.0.0.1:8791` work. The helper must answer CORS (`Access-Control-Allow-Origin: *`,
allow any header, answer `OPTIONS` with 204).

## Transport

Helper listens on `http://127.0.0.1:8791` (override with `CP_ASSISTANT_PORT`).

| Method | Path | Body | Answer |
|---|---|---|---|
| GET | `/health` | | `{ ok, version, stt: 'loading'\|'ready'\|'error', llm: same, listening: bool, inputs: [...] }` |
| GET | `/events` | | Server-sent events, one JSON object per `data:` line (below). Sends `status` on connect and every 10 s as a heartbeat. |
| POST | `/catalog` | `{ actions: [ActionSpec] }` | `{ ok }`. The panel sends this on connect. The helper builds its LLM tool list from it, never from a copy of its own. |
| POST | `/context` | `SessionSnapshot` | `{ ok }`. Sent on connect and (debounced 500 ms) whenever the session changes. |
| POST | `/listen` | `{ inputs: [ListenInput], enabled: bool }` | `{ ok, opened: [...], errors: [...] }` |
| POST | `/prompt` | `{ text, source: 'text' }` | `{ ok, planIds }`. Typed prompts take the same path as speech; plans arrive over `/events`. |
| POST | `/feedback` | `Feedback` | `{ ok }` |
| GET | `/profile` | | the profile JSON (recipes, preferences) |
| POST | `/profile/recipe` | `{ name, aliases?, steps: [Step], description? }` | `{ ok }`. Save or replace a named setup. |
| DELETE | `/profile/recipe/<name>` | | `{ ok }` |

## Events (`/events`)

```jsonc
{ "type": "status", "stt": "ready", "llm": "ready", "listening": true, "inputs": ["MIC/LINE 1"], "audioErrors": [] }
{ "type": "transcript", "id": "u17", "text": "can we get a send off kick and snare", "final": false, "input": "MIC/LINE 1", "t": 1730200000.1 }
{ "type": "transcript", "id": "u17", "text": "can we get a send off kick and snare to a compression bus", "final": true, ... }
{ "type": "thinking", "utteranceId": "u17" }
{ "type": "plan", "plan": Plan }
{ "type": "retract", "planId": "p9", "reason": "superseded" }   // a later utterance replaced it
{ "type": "error", "message": "..." }
{ "type": "levels", "levels": { "MIC/LINE 1": -23.5, "TALKBACK 1": -61.0 } }   // dBFS peak per ListenInput.name; null when silent/closed
```

`levels` drives the panel's meter bridge. The helper takes the block peak of every open input in
its PortAudio callback and publishes at most 10 times a second while listening, only when some
input moved by 1 dB or more. Values are `20·log10(peak)` clamped to −60…0 dBFS (`null` at the
floor or for a closed input), with peak hold and a 20 dB/s fall done by the helper, so the panel
only draws. When listening stops it sends one event with every value `null`. An older helper
sends no `levels`, and the meters stay unlit.

## Plan

```jsonc
{
  "id": "p9",
  "utteranceId": "u17",          // null for typed prompts
  "heard": "create a new send off of kick and snare to a compression bus, load a dbx160 on it",
  "title": "Kick + Snare → Comp Bus (dbx 160)",
  "confidence": 0.86,            // 0..1; the panel dims < 0.5
  "steps": [Step, ...],
  "alternatives": [ { "title": "...", "steps": [...] } ],   // optional, e.g. click cut cues vs everywhere
  "notes": "Bus created stereo."  // optional, short
}
```

`Step` = `{ "action": "<ActionSpec.name>", "args": { ... } }`. The panel validates each step
against its catalog before showing the card; an invalid plan shows as an error card, never runs.

## ActionSpec (sent by the panel in `/catalog`)

```jsonc
{ "name": "addSend", "description": "...", "params": { JSON Schema object } }
```

Tracks, buses and plugins are referred to **by name** (as the user says them; the panel
resolves names case-insensitively, then by prefix, then fuzzily). Inputs are referred to by
number (`"1"`, `"5-6"` for a stereo pair) or by LUNA input name. Positions are bars (1-based)
or marker names.

## SessionSnapshot (sent by the panel in `/context`)

```jsonc
{
  "session": { "name": "Test", "tempo": 120, "meter": "4/4", "playStartBar": 1, "locationBar": 9, "playing": false, "recording": false },
  "tracks": [ { "name": "Kick", "type": "audio", "format": "mono", "input": "MIC/LINE 1", "output": ["Drum Bus"],
                "sends": [ { "slot": 0, "bus": "Comp Bus" } ], "plugins": ["dbx 160 Compressor"], "recordArmed": true,
                "assistantArmed": false, "playlist": "V2", "selected": false } ],
  "buses": [ { "name": "Comp Bus", "format": "stereo", "plugins": [] } ],
  "inputs": [ { "number": "1", "name": "MIC/LINE 1", "format": "mono" }, { "number": "1-2", "name": "MIC/LINE HIZ 1/2", "format": "stereo" } ],
  "markers": [ { "name": "Chorus", "bar": 17 } ],
  "clickOutputs": ["MON", "CUE 1", "CUE 2"]
}
```

## ListenInput (sent by the panel in `/listen`)

The inputs of the LUNA track(s) armed for the assistant (the "Assistant" track has input(s) and
no output). `channel` is the 0-based hardware channel on the audio device, derived by the panel
from LUNA's input order; the helper's config may override it per input name. Inputs the panel
can't number (TALKBACK, MON, VIRTUAL…) arrive with `channel: null`: the helper needs a
`channels` entry for them in its `config.json` (e.g. `"TALKBACK 1": 14` on an Apollo Twin X) and
otherwise reports them in `/listen`'s `errors` instead of guessing a channel.

```jsonc
{ "name": "MIC/LINE 1", "device": "Universal Audio Thunderbolt", "channel": 0, "format": "mono" }
```

## Feedback (panel → helper, the learning signal)

```jsonc
{ "planId": "p9", "outcome": "go" | "dismissed" | "failed", "steps": [Step],   // what actually ran
  "error": "...",                                                                  // when failed
  "edits": [ { "kind": "rename", "from": "Kick", "to": "Kick In" } ] }             // operator changes to what the plan made, seen within 2 min after Go
```
