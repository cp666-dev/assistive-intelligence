"""End-to-end smoke test against a running helper started with --debug.

    ./run.sh --no-capture --debug &      # wait for llm=ready
    uv run python smoke/smoke.py

Posts the sample catalog + snapshots from tests/fixtures, runs the three reference prompts,
feeds chatter / requests through the gate, and one spoken request through VAD + Parakeet.
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

PROMPTS = [
    ("snapshot0.json", "Set me up mono audio tracks for kick, snare, hats, ride, a stereo track for overheads. inputs are 1-6 in that order and a collective stereo output is fine and then create a group that controls all of them"),
    ("snapshot1.json", "create a new send off of kick and snare to a compression bus, load a dbx160 on it"),
    ("snapshot2.json", "ok that drum take was great, mark it as a favourite and then create a new playlist to record, give me extra bars of intro before the drummer comes in then cut the click at the end of the part"),
]
CHATTER = [
    "man that was a long drive",
    "who wants coffee",
    "that snare sounds amazing in here",
    "I think the second verse was dragging a little",
    "did you guys catch the game last night",
]
REQUESTS = [
    "can we get more intro bars before the drums come in",
    "throw an eleven seventy six on the vocal",
    "let's do another one on a new playlist",
]

events: list[dict] = []


def listen():
    with httpx.stream("GET", URL + "/events", timeout=None) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                ev["_rx"] = time.time()
                events.append(ev)


def wait_for(pred, timeout=60.0):
    end = time.time() + timeout
    seen = 0
    while time.time() < end:
        for ev in events[seen:]:
            if pred(ev):
                return ev
        time.sleep(0.02)
    return None


def main():
    h = httpx.get(URL + "/health").json()
    print("health:", h)
    while h["llm"] != "ready" or h["stt"] != "ready":
        time.sleep(1)
        h = httpx.get(URL + "/health").json()
    threading.Thread(target=listen, daemon=True).start()
    time.sleep(0.5)
    catalog = json.loads(Path(os.environ.get("CP_SMOKE_CATALOG", FIX / "catalog.json")).read_text())
    httpx.post(URL + "/catalog", json=catalog).raise_for_status()
    results = {"prompts": [], "gate": [], "voice": None}
    time.sleep(6)  # the panel posts its catalog on connect; let the helper pre-compute that prompt

    for snap, text in PROMPTS:
        httpx.post(URL + "/context", json=json.loads((FIX / snap).read_text())).raise_for_status()
        t0 = time.time()
        pid = httpx.post(URL + "/prompt", json={"text": text, "source": "text"}).json()["planIds"][0]
        ev = wait_for(lambda e: (e["type"] == "plan" and e["plan"]["id"] == pid) or
                      (e["type"] == "error" and e.get("planId") == pid), 90)
        dt = time.time() - t0
        rec = {"prompt": text, "latencyS": round(dt, 2), "event": ev}
        results["prompts"].append(rec)
        print(f"\n=== {dt:.2f}s  {text[:70]}...")
        print(json.dumps(ev and (ev.get("plan") or ev), indent=1))
        if ev and ev["type"] == "plan":
            print("timing:", ev.get("timing"))
            httpx.post(URL + "/feedback", json={"planId": pid, "outcome": "go", "steps": ev["plan"]["steps"]})

    for text, want in [(c, False) for c in CHATTER] + [(r, True) for r in REQUESTS]:
        t0 = time.time()
        uid = httpx.post(URL + "/debug/utterance", json={"text": text, "input": "MIC/LINE 8"}).json()["utteranceId"]
        ev = wait_for(lambda e: e["type"] == "thinking" and e.get("utteranceId") == uid, 4.0)
        acted = ev is not None
        plan = None
        if acted:
            plan = wait_for(lambda e: e["type"] in ("plan",) and e["plan"].get("utteranceId") == uid, 60)
        gate_s = None
        for e in reversed(events):
            if e["type"] == "plan" and e["plan"].get("utteranceId") == uid:
                gate_s = e.get("timing", {}).get("gateS")
        results["gate"].append({"text": text, "expected": want, "acted": acted, "ok": acted == want,
                                "plan": plan and plan["plan"], "gateS": gate_s, "s": round(time.time() - t0, 2)})
        print(f"gate {'OK ' if acted == want else 'BAD'} acted={acted!s:5} {text}" +
              (f"  -> {plan['plan']['title']} ({plan['timing'].get('totalS')}s)" if plan else ""))
        if plan:  # dismiss so it does not linger as pending
            httpx.post(URL + "/feedback", json={"planId": plan["plan"]["id"], "outcome": "dismissed"})

    # Voice: synthesised speech through VAD + Parakeet + gate + planner
    with tempfile.TemporaryDirectory() as td:
        aiff = Path(td) / "v.aiff"
        wav = Path(td) / "v.wav"
        subprocess.run(["say", "-o", str(aiff), "Put a dee bee ex one sixty on the drum bus please."], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@48000", "-c", "1", str(aiff), str(wav)], check=True)
        n0 = len(events)
        t0 = time.time()
        httpx.post(URL + "/debug/feed-file", json={"path": str(wav), "input": "MIC/LINE 8"}, timeout=60)
        fin = wait_for(lambda e: e["type"] == "transcript" and e["final"] and e["input"] == "MIC/LINE 8"
                       and e["_rx"] >= t0, 30)
        plan = wait_for(lambda e: e["type"] == "plan" and fin and e["plan"].get("utteranceId") == fin["id"], 60)
        partials = [e["text"] for e in events[n0:] if e["type"] == "transcript" and not e["final"]]
        results["voice"] = {"partials": partials, "final": fin and fin["text"], "plan": plan and plan["plan"],
                            "timing": plan and plan.get("timing")}
        print("\nvoice partials:", partials)
        print("voice final:", fin and fin["text"])
        print("voice plan:", json.dumps(plan and plan["plan"], indent=1), plan and plan.get("timing"))

    OUT.mkdir(exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print("\nwrote", OUT / "results.json")
    bad = [g for g in results["gate"] if not g["ok"]]
    sys.exit(1 if bad or any(p["event"] is None or p["event"]["type"] != "plan" for p in results["prompts"]) else 0)


if __name__ == "__main__":
    main()
