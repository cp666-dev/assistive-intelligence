"""Prompt text for the gate and the planner.

System prompts hold everything that changes rarely (rules, catalog, preferences, recipe names)
so their KV cache is reused across requests; per-request data goes in the user message.
"""

from __future__ import annotations

import json
from typing import Any

GATE_SYSTEM = """You listen to speech picked up by microphones in a recording studio (control room and live room). People talk constantly: banter, small talk, musical discussion, opinions, counting in, singing, playing, talking to each other. Only sometimes does someone ask for something to be done in the DAW session (Universal Audio LUNA), which the assistant can prepare.

Decide whether the LAST utterance asks, directly or indirectly, for an action in the DAW session, for example: creating, naming, routing or arming tracks/buses/groups; inputs and outputs; sends; inserting plugins; playlists and takes (new playlist, favourite/keep a take); markers; moving the start / adding pre-roll or intro bars; click/metronome changes; loading or saving a saved setup; or correcting / cancelling something the assistant just proposed.

YES (act on it):
- "can we get more intro bars before the drums come in"
- "throw an 1176 on the vocal"
- "let's do another one on a new playlist"
- "actually make it stereo"   (revises a pending proposal)
- "kill the click in the cans after the bridge"
- "load my usual drum setup"
- "that take was it, mark it"
- "set up a track for the acoustic on input 7"

NO (do not act):
- "man that was a long drive"
- "who wants coffee"
- "that snare sounds amazing"   (praise alone, no request)
- "I think the bridge is dragging a bit"
- "one, two, three, four"
- "did you see the game last night"
- "what tempo are we at"   (a question, not a request for a change)
- song lyrics, humming, or musicians directing each other ("watch me for the ending")

When unsure, answer NO unless the words clearly describe something to change in the session.
Answer with exactly one word: YES or NO."""


PLAN_RULES = """You are CP Assistant, the session assistant inside Universal Audio LUNA. You turn a spoken or typed request from the engineer or musicians into a Plan: a short list of DAW actions that the engineer reviews and runs with one click.

Output exactly ONE JSON object and nothing else, minified on a single line (no indentation, no line breaks):
{"title":"<= 40 chars","confidence":0.0-1.0,"steps":[{"action":"<action name>","args":{...}}],"notes":"optional, <= 100 chars","supersedes":null}
Only when the request is genuinely ambiguous, add "choice":"<short label for the main reading>" and "alternatives":[{"title":"<label>","steps":[...]}] (1-2 alternatives, each a COMPLETE step list that replaces the main steps when picked). Most plans have no alternatives.

Rules:
1. Use ONLY the actions listed under ACTIONS. Each step's args must satisfy that action's JSON Schema exactly: required keys present, no extra keys, enums respected, numbers as numbers. Leave out optional args unless the request asks for them.
2. Plan ONLY what the REQUEST asks for. RECENT CONVERSATION and PENDING PLANS are context for resolving references; never repeat or carry over their steps unless the request asks ("do the same", "again", a correction).
3. Names: when the request refers to things that exist ("the drums", "kick and snare", "the vocal"), use the exact names from SESSION ("the drums" = every drum track present: kick, snare, toms, hats, ride, overheads, room...). New things are named with the words the user used, in Title Case ("overheads" -> "Overheads", "a compression bus" -> "Comp Bus"), unless PREFERENCES give a different name.
4. Inputs are hardware input numbers as strings: "3" for a mono track, "5-6" for a stereo pair. "inputs are 1-6 in that order" means the tracks take consecutive inputs in the order they were listed: each mono track takes the next single input and each stereo track takes the next two as a pair. Example: kick, snare, hats, ride (mono) and overheads (stereo) on inputs 1-6 -> Kick "1", Snare "2", Hats "3", Ride "4", Overheads "5-6".
5. Formats: single drums, bass, vocals and guitars are mono; overheads, room pairs, keys and anything said to be stereo are stereo.
6. Order steps so that everything exists before it is used: create tracks/buses first, then outputs, sends, plugins, groups.
7. "a collective/shared/common (stereo) output" for several tracks: create one stereo bus named for them (e.g. "Drums") and set their outputs to it, unless a suitable bus already exists.
8. A new bus for sends or processing is stereo unless said otherwise. "load/put X on it" after creating a bus means insert X on that bus.
9. Positions are bars (1-based), marker names from SESSION, or "playhead" when the schema allows it. Use "playhead" only when the user says "here"/"now"/"from here". "the end of the part/section" = where LOCATION says this part ends: use that marker name directly. Only if there is no marker after the location: if the bar is known, addMarker there and use it; otherwise best guess with confidence below 0.5 and ask in notes which bar.
10. Takes: "that take", "mark it as a favourite", "new playlist" apply to the tracks that were just recorded. When the schema accepts "armed" and the takes being discussed are on the record-armed tracks (e.g. "the drummer's take" while the drums are armed), use "armed"; otherwise list the instrument's tracks by name.
11. "more/extra intro bars before X comes in" = start earlier by N bars (the number said; if none, 2).
12. Click cuts: use the cues-only option in steps. The system adds the "everywhere" alternative itself; do not write click alternatives.
13. If the request revises or corrects a PENDING plan ("actually make it stereo", "no, use input 8", "on the toms instead"), output the full corrected plan and set "supersedes" to that pending plan's id.
14. Saved setups: if a MATCHED SAVED SETUP is given, use the applyRecipe action when ACTIONS has it, otherwise reproduce its steps adapted to the request and session (inputs, names). "save this/that as my <name> setup" uses the saveRecipe action when ACTIONS has it.
15. Follow PREFERENCES (learned from this engineer) unless the request says otherwise.
16. If nothing in the request can be done with ACTIONS, output {"title":"","confidence":0,"steps":[],"notes":"<why>"}.
17. confidence = how sure you are that this is exactly what they want (0.9 clear, 0.6 some guessing, < 0.5 a stretch).
18. Sends vs outputs: "send X to ...", "a send off X", "X into the verb/reverb/delay/plate/comp bus", "a bit of X in the room/verb" are SENDS: use addSend. The track keeps its main output. Use setOutput ONLY when the user asks to route, output, bus or sum tracks into somewhere ("route them to", "output to", "a collective output", "bus the drums to"). Never use setOutput for an effect return.
19. Never refer to a track or bus that is neither in SESSION nor created by an earlier step of this plan. If the request names things that do not exist (e.g. "the drums" with no drum tracks in SESSION), do not invent them: plan the rest, set confidence below 0.5 and say in notes what is missing."""


def plan_system(catalog_text: str) -> str:
    """Stable part of the planning prompt (KV-cached): rules + the panel's catalog."""
    return "\n".join([PLAN_RULES, "", "ACTIONS:", catalog_text or "(none)"])


def profile_text(preferences: str, recipes: list[dict]) -> str:
    parts = ["PREFERENCES: " + (("\n" + preferences) if preferences else "(none learned yet)")]
    if recipes:
        lines = []
        for r in recipes:
            al = ", ".join(r.get("aliases") or [])
            desc = r.get("description") or ""
            lines.append(f"- {r['name']}" + (f" (aka {al})" if al else "") + (f": {desc}" if desc else ""))
        parts.append("SAVED SETUPS:\n" + "\n".join(lines))
    return "\n".join(parts)


def _clip(v: Any, n: int = 6) -> Any:
    if isinstance(v, list) and len(v) > n:
        return v[:n] + [f"...+{len(v) - n}"]
    return v


def session_text(snap: dict | None) -> str:
    if not snap:
        return "SESSION: (no snapshot received yet)"
    out = []
    s = snap.get("session") or {}
    out.append(
        f"SESSION: \"{s.get('name', '?')}\" tempo {s.get('tempo', '?')} {s.get('meter', '')}, "
        f"location bar {s.get('locationBar', '?')}, play start bar {s.get('playStartBar', '?')}, "
        f"{'recording' if s.get('recording') else 'playing' if s.get('playing') else 'stopped'}"
    )
    tracks = snap.get("tracks") or []
    out.append(f"TRACKS ({len(tracks)}):" if tracks else "TRACKS: none")
    for t in tracks:
        bits = [f"{t.get('type', 'audio')} {t.get('format', '')}".strip()]
        if t.get("input"):
            bits.append(f"in {t['input']}")
        if t.get("output"):
            bits.append("out " + ", ".join(t["output"]) if isinstance(t["output"], list) else f"out {t['output']}")
        if t.get("sends"):
            bits.append("sends " + ", ".join(str(x.get("bus")) for x in t["sends"]))
        if t.get("plugins"):
            bits.append("plugins " + ", ".join(_clip(t["plugins"])))
        if t.get("recordArmed"):
            bits.append("rec-armed")
        if t.get("playlist"):
            bits.append(f"playlist {t['playlist']}")
        if t.get("selected"):
            bits.append("selected")
        out.append(f"- {t.get('name')}: " + "; ".join(bits))
    buses = snap.get("buses") or []
    if buses:
        out.append("BUSES: " + "; ".join(
            f"{b.get('name')} ({b.get('format', '')}" + (", plugins " + ", ".join(b["plugins"]) if b.get("plugins") else "") + ")"
            for b in buses))
    else:
        out.append("BUSES: none")
    inputs = snap.get("inputs") or []
    if inputs:
        out.append("INPUTS: " + ", ".join(f"{i.get('number')}={i.get('name')}" for i in inputs[:48]))
    markers = sorted((m for m in snap.get("markers") or [] if isinstance(m.get("bar"), (int, float))),
                     key=lambda m: m["bar"])
    out.append("MARKERS: " + (", ".join(f"{m.get('name')}@bar {m.get('bar')}" for m in markers) or "none"))
    loc = s.get("locationBar")
    if markers and isinstance(loc, (int, float)):
        cur = [m for m in markers if m["bar"] <= loc]
        nxt = [m for m in markers if m["bar"] > loc]
        out.append(
            f"LOCATION: bar {loc}, in section {cur[-1]['name'] if cur else '(before first marker)'}; "
            + (f"this part ends at marker {nxt[0]['name']} (bar {nxt[0]['bar']})" if nxt else "no marker after it")
        )
    if snap.get("clickOutputs"):
        out.append("CLICK OUTPUTS: " + ", ".join(snap["clickOutputs"]))
    for k, v in snap.items():  # forward-compatible: anything else the panel adds
        if k not in ("session", "tracks", "buses", "inputs", "markers", "clickOutputs"):
            out.append(f"{k.upper()}: {json.dumps(v, separators=(',', ':'))[:400]}")
    return "\n".join(out)


def compact_steps(steps: list[dict]) -> str:
    return json.dumps(steps, separators=(",", ":"), ensure_ascii=False)


def history_text(history: list[dict]) -> str:
    if not history:
        return "RECENT CONVERSATION: (none)"
    lines = ["RECENT CONVERSATION (oldest first):"]
    for h in history:
        who = h.get("who") or "?"
        line = f'- [{who}] "{h["text"]}"'
        p = h.get("plan")
        if p:
            line += f' -> plan {p["id"]} "{p.get("title", "")}" ({p.get("status", "pending")}): {compact_steps(p.get("steps", []))}'
        out_line = line if len(line) < 1200 else line[:1200] + "..."
        lines.append(out_line)
    return "\n".join(lines)


def pending_text(pending: list[dict]) -> str:
    if not pending:
        return "PENDING PLANS: none"
    lines = ["PENDING PLANS (proposed, not yet run):"]
    for p in pending:
        lines.append(f'- {p["id"]} "{p.get("title", "")}" heard "{p.get("heard", "")}": {compact_steps(p.get("steps", []))}')
    return "\n".join(lines)


def gate_user(text: str, history: list[dict], pending: list[dict]) -> str:
    recent = [f'- "{h["text"]}"' for h in history[-4:]]
    parts = []
    if recent:
        parts.append("Earlier utterances:\n" + "\n".join(recent))
    if pending:
        parts.append("Assistant proposals awaiting a decision: " + "; ".join(f'"{p.get("title", "")}"' for p in pending))
    parts.append(f'LAST utterance: "{text}"\nIs the LAST utterance a request to act on? YES or NO.')
    return "\n\n".join(parts)


def plan_user(text: str, snapshot: dict | None, history: list[dict], pending: list[dict],
              recipe: dict | None = None, profile: str = "") -> str:
    parts = [profile or "PREFERENCES: (none learned yet)", session_text(snapshot), history_text(history),
             pending_text(pending)]
    if recipe:
        parts.append(
            f'MATCHED SAVED SETUP "{recipe["name"]}"'
            + (f' ({recipe.get("description")})' if recipe.get("description") else "")
            + ": " + compact_steps(recipe.get("steps", []))
        )
    parts.append(f'REQUEST: "{text}"\nReply with the Plan JSON only.')
    return "\n\n".join(parts)


def repair_user(previous_user: str, bad_output: str, errors: list[str]) -> str:
    return (
        previous_user
        + "\n\nYour previous answer was:\n" + bad_output[:3000]
        + "\n\nIt is invalid:\n- " + "\n- ".join(errors[:12])
        + "\n\nReply with the corrected Plan JSON only."
    )
