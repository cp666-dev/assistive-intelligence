"""Which spoken requests work, reliably and quickly: each phrase, in several macOS voices, through
the live path (VAD -> Parakeet -> gate -> planner) of a helper started with --debug.

    ./run.sh --no-capture --debug &      # wait for llm=ready
    uv run python smoke/phrases.py [runs per voice, default 1]

A phrase passes when every voice's speech produced a plan whose actions are the expected ones.
Prints a table, writes smoke/out/phrases.json, and exits 1 if any phrase failed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

URL = os.environ.get("CP_ASSISTANT_URL", "http://127.0.0.1:8791")
FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
OUT = Path(__file__).resolve().parent / "out"
VOICES = ["Samantha", "Daniel", "Karen"]

# (session snapshot, spoken phrase, the actions its plan must use)
PHRASES = [
    ("snapshot0.json", "Set up mono tracks for kick, snare and hi-hat, and a stereo overheads track.", {"createTracks"}),
    ("snapshot0.json", "Add a stereo drum bus.", {"createBus"}),
    ("snapshot1.json", "Send the kick and snare to a new compression bus.", {"createBus", "addSend"}),
    ("snapshot1.json", "Route all the drums to the drum bus.", {"setOutput"}),
    ("snapshot1.json", "Group the kick, snare and hats together.", {"createGroup"}),
    ("snapshot1.json", "Colour the drum tracks red.", {"setTrackColor"}),
    ("snapshot1.json", "Mute the vocal.", {"setMute"}),
    ("snapshot1.json", "Record arm the vocal.", {"setRecordArm"}),
    ("snapshot1.json", "Rename the vocal track to Lead Vox.", {"renameTrack"}),
    ("snapshot1.json", "Turn the overheads down six dB.", {"setGain"}),
    ("snapshot0.json", "Set the tempo to one hundred and twenty.", {"setTempo"}),
    ("snapshot0.json", "Add a marker called chorus at bar nine.", {"addMarker"}),
    ("snapshot0.json", "Loop bars five to nine.", {"setLoop"}),
    ("snapshot1.json", "Give me two more bars of intro before the drums come in.", {"setStartEarlier"}),
    ("snapshot2.json", "That take was great, mark it as a favourite.", {"favouriteTake"}),
    ("snapshot2.json", "Let's do another take on a new playlist.", {"newPlaylist"}),
    ("snapshot0.json", "Drop a marker here called chorus.", {"addMarker"}),
    ("snapshot0.json", "Put a marker called verse at bar five.", {"addMarker"}),
]
# PHRASES_MATCH=<text>: only phrases containing it (case-insensitive), to rerun a few.
if os.environ.get("PHRASES_MATCH"):
    PHRASES = [p for p in PHRASES if any(m.lower() in p[1].lower() for m in os.environ["PHRASES_MATCH"].split("|"))]

events: list[dict] = []


def listen():
    with httpx.stream("GET", URL + "/events", timeout=None) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                ev["_rx"] = time.time()
                events.append(ev)


def wait_for(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        for ev in list(events):
            if pred(ev):
                return ev
        time.sleep(0.02)
    return None


def speak(text: str, voice: str, td: Path) -> Path:
    aiff, wav = td / "p.aiff", td / f"p-{voice}.wav"
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@48000", "-c", "1", str(aiff), str(wav)], check=True)
    return wav


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    h = httpx.get(URL + "/health").json()
    while h["llm"] != "ready" or h["stt"] != "ready":
        time.sleep(1)
        h = httpx.get(URL + "/health").json()
    threading.Thread(target=listen, daemon=True).start()
    time.sleep(0.5)
    httpx.post(URL + "/catalog", json=json.loads((FIX / "catalog.json").read_text())).raise_for_status()
    time.sleep(8)  # the helper pre-computes the planner prompt for the catalog

    results = []
    with tempfile.TemporaryDirectory() as tdn:
        td = Path(tdn)
        for snap, text, want in PHRASES:
            httpx.post(URL + "/context", json=json.loads((FIX / snap).read_text())).raise_for_status()
            trials = []
            for voice in VOICES:
                wav = speak(text, voice, td)
                for _ in range(runs):
                    httpx.post(URL + "/debug/reset").raise_for_status()  # each trial is a fresh request
                    t0 = time.time()
                    httpx.post(URL + "/debug/feed-file", json={"path": str(wav), "input": "MIC/LINE 8"}, timeout=60)
                    fin = wait_for(lambda e: e["type"] == "transcript" and e["final"] and e["_rx"] >= t0, 30)
                    plan = fin and wait_for(lambda e: e["type"] == "plan" and e["plan"].get("utteranceId") == fin["id"], 45)
                    steps = (plan or {}).get("plan", {}).get("steps") or []
                    acts = {s.get("action") for s in steps}
                    ok = bool(plan) and acts == want and (plan["plan"].get("confidence") or 0) >= 0.6
                    trials.append({
                        "voice": voice, "heard": fin and fin["text"], "ok": ok, "actions": sorted(a for a in acts if a),
                        "title": plan and plan["plan"].get("title"), "confidence": plan and plan["plan"].get("confidence"),
                        "steps": steps, "totalS": plan and (plan.get("timing") or {}).get("totalS"),
                    })
                    if plan:  # dismissed, so it isn't pending context for the next phrase
                        httpx.post(URL + "/feedback", json={"planId": plan["plan"]["id"], "outcome": "dismissed"})
                    time.sleep(0.5)
            passed = all(t["ok"] for t in trials)
            worst = max((t["totalS"] or 99) for t in trials)
            results.append({"phrase": text, "want": sorted(want), "passed": passed, "worstS": worst, "trials": trials})
            print(f"{'PASS' if passed else 'FAIL'}  {worst:5.1f}s  {text}")
            if not passed:
                for t in trials:
                    if not t["ok"]:
                        print(f"        {t['voice']}: heard {t['heard']!r} -> {t['actions']} conf={t['confidence']}")
    OUT.mkdir(exist_ok=True)
    (OUT / "phrases.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print("\nwrote", OUT / "phrases.json")
    sys.exit(0 if all(r["passed"] for r in results) else 1)


if __name__ == "__main__":
    main()
